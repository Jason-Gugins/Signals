"""Resilient concurrent collector runner."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from loguru import logger

from src.core.models import Account, Document
from src.core.runlog import RunContext
from src.signals.normalize import normalize_batch
from src.sources.base import FetchTask, SourceAdapter


@dataclass
class RunnerStats:
    tasks: int = 0
    fetched: int = 0
    cached: int = 0
    failed: int = 0
    skipped: int = 0
    candidates: int = 0
    signals_new: int = 0
    by_source: dict = field(default_factory=dict)

    def _src(self, key: str) -> dict:
        return self.by_source.setdefault(
            key, {"tasks": 0, "fetched": 0, "cached": 0, "failed": 0, "skipped": 0, "candidates": 0, "signals_new": 0}
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


class CollectorRunner:
    def __init__(self, config, db, registry, store, fetcher, signal_store, taxonomy, ctx: RunContext, browser=None, cloudflare_bypass=None, datadome_bypass=None, stealth_browser=None):
        self.config = config
        self.db = db
        self.registry = registry
        self.store = store
        self.fetcher = fetcher
        self.signal_store = signal_store
        self.taxonomy = taxonomy
        self.ctx = ctx
        self.browser = browser
        self.cloudflare_bypass = cloudflare_bypass
        self.datadome_bypass = datadome_bypass
        self.stealth_browser = stealth_browser

    def run(
        self,
        adapters: list[SourceAdapter],
        accounts: list[Account],
        *,
        force: bool = False,
        dry_run: bool = False,
        max_passes: int = 2,
        limit_per_source: int | None = None,
    ) -> RunnerStats:
        stats = RunnerStats()
        now = _now()
        try:
            for adapter in adapters:
                if adapter.key == "marketplace_g2":
                    try:
                        g2_cfg = self.config.load_yaml("marketplace").get("sites", {}).get("g2", {})
                        cookie_file = g2_cfg.get("session_cookie_file")
                        if cookie_file and hasattr(adapter, "_session_cookie_file"):
                            adapter._session_cookie_file = cookie_file
                    except Exception:
                        pass
            for adapter in adapters:
                eligible = []
                for account in accounts:
                    if not _requires_met(adapter, account):
                        stats.skipped += 1
                        stats._src(adapter.key)["skipped"] += 1
                        continue
                    cur = self._cursor(adapter.key, _ckey(adapter, account))
                    if not force and cur:
                        if int(cur.get("fail_count") or 0) >= 5:
                            stats.skipped += 1
                            stats._src(adapter.key)["skipped"] += 1
                            continue
                        due = cur.get("next_due_at")
                        if due and due > _iso(now):
                            stats.skipped += 1
                            stats._src(adapter.key)["skipped"] += 1
                            continue
                    eligible.append(account)
                if getattr(adapter, "fanout", False) and eligible:
                    self._run_fanout(adapter, eligible, stats, force=force, dry_run=dry_run, now=now)
                    continue
                for account in eligible:
                    try:
                        self._run_pair(adapter, account, stats, force=force, dry_run=dry_run, max_passes=max_passes, limit_per_source=limit_per_source, now=now)
                    except Exception as exc:
                        logger.exception("adapter {} failed for {}", adapter.key, account.domain)
                        self._record_fail(adapter.key, account.domain, exc)
                        stats.failed += 1
                        stats._src(adapter.key)["failed"] += 1
                        self.ctx.bump(errors=1)
        finally:
            stealth = getattr(self, "stealth_browser", None)
            if stealth is not None:
                try:
                    stealth.close()
                except Exception:
                    logger.warning("stealth browser close() failed")
        return stats

    def _run_pair(self, adapter, account, stats, *, force, dry_run, max_passes, limit_per_source, now):
        key = _ckey(adapter, account)
        cursor_row = self._cursor(adapter.key, key) or {}
        cursor = cursor_row.get("cursor")
        pending_follow: list[FetchTask] = []
        if adapter.key == "marketplace_g2":
            try:
                g2_cfg = self.config.load_yaml("marketplace").get("sites", {}).get("g2", {})
                max_passes = int(g2_cfg.get("max_review_pages", 5)) + 1
            except Exception:
                max_passes = 6
        for pass_i in range(max_passes):
            if pending_follow:
                tasks = pending_follow
                pending_follow = []
            else:
                tasks = adapter.plan(account, cursor)
            if limit_per_source:
                tasks = tasks[:limit_per_source]
            if dry_run:
                stats.tasks += len(tasks)
                stats._src(adapter.key)["tasks"] += len(tasks)
                logger.info("dry_run {} {} {} tasks", adapter.key, account.domain, len(tasks))
                return
            results = self._execute_tasks(adapter, tasks, cursor_row, stats)
            last_doc = None
            all_cands = []
            follow: list[FetchTask] = []
            tech_harvests: list = []
            for task, result in results:
                if result is None:
                    continue
                if result.status == 304:
                    stats.cached += 1
                    stats._src(adapter.key)["cached"] += 1
                    continue
                # If the bypass ran but failed (cloudflare_unsolved=True),
                # let the result through to the collector so it can record
                # cloudflare-only.  Don't crash on the 403.
                _cf_unsolved = getattr(result, "_cloudflare_unsolved", None)
                if _cf_unsolved:
                    if result.doc is None:
                        # Create a minimal doc with the challenge body so the
                        # collector can parse it (will record cloudflare-only).
                        result = type(result)(
                            ok=True, status=200,
                            doc=self.store.put(
                                source=task.source, url=task.url,
                                body=b"<html><title>Just a moment...</title></html>",
                                content_type="text/html", status=200,
                                domain=task.domain,
                            ),
                            cached=False, error=None, elapsed_ms=0,
                        )
                    else:
                        # We have the 403 body in a doc — make it pass the
                        # ok check so the collector can process it.
                        result = type(result)(
                            ok=True, status=200, doc=result.doc,
                            cached=False, error=None, elapsed_ms=0,
                            cloudflare_cookies=result.cloudflare_cookies,
                        )
                        result._cloudflare_unsolved = True
                if not result.ok or result.doc is None:
                    raise RuntimeError(result.error or f"fetch failed {result.status}")
                stats.fetched += 1
                stats._src(adapter.key)["fetched"] += 1
                self.ctx.bump(documents=1)
                last_doc = result.doc
                meta = dict(task.meta or {})
                meta.setdefault("today", now.date().isoformat())
                meta.setdefault("registry", self.registry)
                # Propagate the cloudflare_unsolved flag set by _fetch_one
                # when a challenge was detected and the bypass was attempted.
                if _cf_unsolved is not None:
                    meta["cloudflare_unsolved"] = _cf_unsolved
                if adapter.key == "federal_register" and "watches" not in meta:
                    try:
                        meta["watches"] = (self.config.load_yaml("regulations").get("watches") or [])
                    except Exception:
                        meta["watches"] = []
                if adapter.key == "marketplace_g2":
                    try:
                        g2_cfg = self.config.load_yaml("marketplace").get("sites", {}).get("g2", {})
                        meta.setdefault("review_lookback_days", g2_cfg.get("review_lookback_days", 90))
                        meta.setdefault("max_review_pages", g2_cfg.get("max_review_pages", 5))
                        meta.setdefault("click_show_more", g2_cfg.get("deep_reviews", False))
                    except Exception:
                        meta.setdefault("review_lookback_days", 90)
                cands = adapter.parse(result.doc, account, meta)
                all_cands.extend(cands)
                follow.extend(adapter.follow_tasks(result.doc, account, meta) or [])
                jobs = adapter.harvest_jobs(result.doc, account, meta) or []
                self._persist_jobs(adapter, account, jobs, now, more_pages=bool(follow))
                harvest = getattr(adapter, "harvest_tech", None)
                if callable(harvest):
                    tech_harvests.append(harvest(result.doc, account, meta) or [])
                harvest_revs = getattr(adapter, "harvest_reviews", None)
                if callable(harvest_revs):
                    from src.sources.marketplace.collector import upsert_g2_reviews
                    revs = harvest_revs(result.doc, account, meta) or []
                    if revs:
                        upsert_g2_reviews(self.db, revs, now=_iso(now), raw_ref=result.doc.doc_id)
            if tech_harvests:
                from src.sources.techstack.collector import upsert_technologies
                from src.sources.techstack.fingerprint import merge_matches

                upsert_technologies(
                    self.db, account.domain, merge_matches(*tech_harvests), now=_iso(now)
                )
            stats.candidates += len(all_cands)
            stats._src(adapter.key)["candidates"] += len(all_cands)
            new_n = self._persist(account, adapter.key, all_cands, last_doc)
            stats.signals_new += new_n
            stats._src(adapter.key)["signals_new"] += new_n
            if last_doc is not None:
                cursor = adapter.next_cursor(last_doc, all_cands)
                self._record_success(adapter, key, cursor, last_doc, now)
            elif not results:
                self._record_success(adapter, key, cursor, None, now)
                break
            if follow and pass_i + 1 < max_passes:
                pending_follow = follow
                continue
            if pass_i + 1 < max_passes and cursor and last_doc is not None:
                continue
            break
        extra = adapter.local_harvest(
            db=self.db, account=account, today=now.date(),
            task_meta={"today": now.date().isoformat(), "registry": self.registry},
        ) or []
        if extra:
            added = self._persist(account, adapter.key, extra, None)
            stats.signals_new += added
            stats._src(adapter.key)["signals_new"] += added
            stats.candidates += len(extra)

    def _run_fanout(self, adapter, accounts, stats, *, force, dry_run, now):
        # one plan from the first account; parse per account
        cursor_row = self._cursor(adapter.key, "global") or {}
        tasks = adapter.plan(accounts[0], cursor_row.get("cursor"))
        if dry_run:
            stats.tasks += len(tasks)
            return
        results = self._execute_tasks(adapter, tasks, cursor_row, stats)
        last_doc = None
        for task, result in results:
            if result is None or result.status == 304:
                if result and result.status == 304:
                    stats.cached += 1
                continue
            if not result.ok or result.doc is None:
                raise RuntimeError(result.error or "fanout fetch failed")
            stats.fetched += 1
            last_doc = result.doc
            for account in accounts:
                meta = dict(task.meta or {})
                meta.setdefault("today", now.date().isoformat())
                meta.setdefault("registry", self.registry)
                if adapter.key == "federal_register" and "watches" not in meta:
                    try:
                        meta["watches"] = (self.config.load_yaml("regulations").get("watches") or [])
                    except Exception:
                        meta["watches"] = []
                cands = adapter.parse(result.doc, account, meta)
                stats.candidates += len(cands)
                new_n = self._persist(account, adapter.key, cands, result.doc)
                stats.signals_new += new_n
                jobs = adapter.harvest_jobs(result.doc, account, meta) or []
                self._persist_jobs(adapter, account, jobs, now, more_pages=False)
        if last_doc is not None:
            self._record_success(adapter, "global", adapter.next_cursor(last_doc, []), last_doc, now)

    def _execute_tasks(self, adapter, tasks, cursor_row, stats) -> list[tuple[FetchTask, object]]:
        stats.tasks += len(tasks)
        stats._src(adapter.key)["tasks"] += len(tasks)
        etag = cursor_row.get("etag")
        last_mod = cursor_row.get("last_modified")
        out: list[tuple[FetchTask, object]] = []
        if adapter.tier == "browser" or any((t.meta or {}).get("capture") == "network" for t in tasks):
            for task in tasks:
                out.append((task, self._fetch_one(task, etag, last_mod)))
            return out
        workers = max(1, int(self.config.http.max_workers or 1))
        if len(tasks) <= 1 or workers == 1:
            return [(t, self._fetch_one(t, etag, last_mod)) for t in tasks]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(self._fetch_one, t, etag, last_mod): t for t in tasks}
            for fut in as_completed(futs):
                task = futs[fut]
                out.append((task, fut.result()))
        return out

    def _fetch_one(self, task, etag, last_mod):
        if (task.meta or {}).get("capture") == "network":
            if self.browser is None:
                return None
            return self.browser.fetch(
                task.url,
                source=task.source,
                domain=task.domain,
                capture_network=True,
                wait_ms=2500,
            )
        # marketplace_g2: G2 serves reviews as client-rendered elv-* DOM in the
        # /products/{slug}/reviews_and_filters fragment (the /reviews page is
        # only the app shell). Prefer the rendered fragment via the DataDome
        # stealth browser so the Document body contains the real reviews.
        if task.source == "marketplace_g2":
            g2_result = self._fetch_g2_fragment(task)
            if g2_result is not None:
                return g2_result
        result = self.fetcher.get(task, etag=etag, last_modified=last_mod)
        # Cloudflare challenge detection for http-tier html tasks.
        # When a bypass is wired and the response body is a CF challenge,
        # route through the bypass waterfall.  The collector gates stripping
        # on the ``cloudflare_unsolved`` meta flag (set below), not on
        # ``is_challenge_evidence`` (which can't see "Just a moment..." HTML
        # because HttpEvidence has no hosts attribute).
        if (
            self.cloudflare_bypass
            and task.source in _CF_BYPASS_SOURCES
            and not (task.meta or {}).get("capture")
            and result.doc is not None
        ):
            from src.sources.techstack.fingerprint import classify_cloudflare_challenge

            body = result.doc.body or b""
            if classify_cloudflare_challenge(status=result.status, body=body):
                try:
                    ua = self.config.resolved_user_agent()
                except Exception:
                    ua = self.config.http.user_agent
                proxy = getattr(self.config.browser, "proxy_server", None) or "direct"
                try:
                    outcome = self.cloudflare_bypass.attempt(
                        domain=task.domain, url=task.url, user_agent=ua, proxy=proxy,
                        source=task.source,
                        click_show_more=bool((task.meta or {}).get("click_show_more")),
                    )
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning("cloudflare bypass failed for {}: {}", task.domain, exc)
                    outcome = None
                if outcome is not None:
                    if outcome.success and outcome.result is not None:
                        result = outcome.result
                    result._cloudflare_unsolved = not (outcome.success and outcome.result is not None)
        # DataDome detection — after CF bypass, check if the body is a DataDome challenge
        from src.sources.techstack.datadome import is_datadome_challenge

        if (
            hasattr(self, "datadome_bypass")
            and self.datadome_bypass
            and result.doc is not None
            and is_datadome_challenge(status=result.status, body=result.doc.body or b"")
        ):
            try:
                ua = self.config.resolved_user_agent()
            except Exception:
                ua = self.config.http.user_agent
            proxy = getattr(self.config.browser, "proxy_server", None) or "direct"
            try:
                dd_outcome = self.datadome_bypass.attempt(
                    domain=task.domain, url=task.url,
                    user_agent=ua, proxy=proxy,
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("datadome bypass failed for {}: {}", task.domain, exc)
                dd_outcome = None
            if dd_outcome is not None and dd_outcome.success and dd_outcome.result_body:
                # Replace the doc body with the bypass result
                result = type(result)(
                    ok=True, status=200,
                    doc=self.store.put(
                        source=task.source, url=task.url,
                        body=dd_outcome.result_body,
                        content_type="text/html", status=200,
                        domain=task.domain,
                    ),
                    cached=False, error=None, elapsed_ms=0,
                )
        return result

    def _fetch_g2_fragment(self, task) -> Optional[object]:
        """Fetch the rendered reviews_and_filters fragment for a marketplace_g2 task.

        G2's current live reviews are served as client-rendered elv-* DOM from
        ``/products/{slug}/reviews_and_filters``; the plain ``/reviews`` page is
        only the app shell with no review cards. When a DataDome stealth
        (Patchright) browser is available, fetch the fragment so the returned
        FetchResult's Document body carries the rendered reviews that
        ``MarketplaceG2Source.parse()``/``harvest_reviews()`` expect.

        Returns the FetchResult on success, or None when no rendered path is
        available (no stealth browser, no product_slug) so the caller falls back
        to the normal challenge->bypass->curl flow. Scoped strictly to
        ``marketplace_g2`` by the caller.
        """
        stealth = None
        datadome_bypass = getattr(self, "datadome_bypass", None)
        if datadome_bypass is not None:
            try:
                stealth = getattr(datadome_bypass, "stealth", None)
            except Exception:  # pragma: no cover - defensive
                stealth = None
        browser = stealth if stealth is not None else getattr(self, "browser", None)
        if browser is None:
            return None
        slug = (task.meta or {}).get("product_slug")
        if not slug:
            return None  # cannot build the fragment URL without the slug
        page = (task.meta or {}).get("page")
        from src.sources.marketplace.g2 import g2_reviews_fragment_url

        url = g2_reviews_fragment_url(slug, page=page)
        # DataDome clears in HEADED mode only (Proof-of-Browser detects the
        # headless SwiftShader renderer), and it scores behavioral signals —
        # warm up like a human (homepage visit + scroll + dwell) before the
        # fragment navigation. The browser is forced headed via config
        # mutation (PatchrightBrowserFetcher launches from config.browser).
        try:
            browser_cfg = getattr(browser, "config", None)
            if browser_cfg is not None and getattr(browser_cfg, "browser", None) is not None:
                browser_cfg.browser.headless = False
        except Exception:  # pragma: no cover - defensive
            pass
        try:
            result = browser.fetch(
                url, source=task.source, domain=task.domain,
                wait_ms=4000, scroll=True,
                warmup_url="https://www.g2.com/", warmup_ms=4000,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("g2 fragment fetch failed for {}: {}", task.domain, exc)
            return None
        if result is not None and result.ok and result.doc is not None and result.doc.body:
            return result
        return None

    def _persist(self, account, source, cands, doc) -> int:
        if not cands:
            return 0
        now = _iso(_now())
        raw_ref = doc.doc_id if doc else None
        valid, _rej = normalize_batch(cands, account=account, source=source, taxonomy=self.taxonomy, now=now, raw_ref=raw_ref)
        new, _upd = self.signal_store.upsert_many(valid)
        self.ctx.bump(signals_new=new)
        return new

    def _persist_jobs(self, adapter, account, jobs, now, *, more_pages: bool) -> None:
        is_ats = str(getattr(adapter, "key", "")).startswith("ats_")
        if not jobs and not is_ats:
            return
        from src.sources.ats.common import snapshot_jobs, upsert_jobs

        upsert_jobs(
            self.db,
            account.domain,
            jobs,
            adapter.key,
            now=_iso(now),
            token=getattr(account, "ats_token", None) or "",
            mark_closed=is_ats and not more_pages,
        )
        if is_ats and not more_pages:
            snapshot_jobs(self.db, account.domain, as_of=now.date().isoformat())

    def _cursor(self, source: str, key: str) -> Optional[dict]:
        return self.db.one("SELECT * FROM source_cursors WHERE source=? AND key=?", (source, key))

    def _record_success(self, adapter, key, cursor, doc, now):
        due = _iso(now + timedelta(hours=int(adapter.cadence_hours or 24)))
        self.db.upsert(
            "source_cursors",
            {
                "source": adapter.key,
                "key": key,
                "cursor": cursor,
                "etag": getattr(doc, "etag", None) if doc else None,
                "last_modified": getattr(doc, "last_modified", None) if doc else None,
                "last_run_at": _iso(now),
                "next_due_at": due,
                "fail_count": 0,
                "last_error": None,
            },
            pk=("source", "key"),
        )

    def _record_fail(self, source, key, exc):
        row = self._cursor(source, key) or {}
        n = int(row.get("fail_count") or 0) + 1
        self.db.upsert(
            "source_cursors",
            {
                "source": source,
                "key": key,
                "cursor": row.get("cursor"),
                "etag": row.get("etag"),
                "last_modified": row.get("last_modified"),
                "last_run_at": row.get("last_run_at"),
                "next_due_at": row.get("next_due_at"),
                "fail_count": n,
                "last_error": str(exc)[:500],
            },
            pk=("source", "key"),
        )


def _ckey(adapter, account) -> str:
    return "global" if getattr(adapter, "fanout", False) else account.domain


ATS_PREFIX = "ats_"
_CF_BYPASS_SOURCES = {"techstack", "marketplace_g2"}
COLLECTED_VENDORS = {
    "greenhouse",
    "lever",
    "ashby",
    "smartrecruiters",
    "workable",
    "recruitee",
    "workday",
}


def _ats_vendor_ok(adapter, account) -> bool:
    if not str(getattr(adapter, "key", "")).startswith(ATS_PREFIX):
        return True
    vendor = (getattr(account, "ats_vendor", None) or "").casefold()
    if vendor not in COLLECTED_VENDORS:
        return False
    return adapter.key == f"{ATS_PREFIX}{vendor}"


def _requires_met(adapter, account) -> bool:
    for field_name in adapter.requires or ():
        if not getattr(account, field_name, None):
            return False
    return _ats_vendor_ok(adapter, account)
