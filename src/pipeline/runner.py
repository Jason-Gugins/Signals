"""Resilient concurrent collector runner."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from loguru import logger

from src.core.errors import BACKOFF_MULTIPLIER, FetchErrorClass, classify_fetch_error
from src.core.filelock import exclusive_lock
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
    def __init__(self, config, db, registry, store, fetcher, signal_store, taxonomy, ctx: RunContext, browser=None, cloudflare_bypass=None, datadome_bypass=None, stealth_browser=None, routing=None):
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
        self.routing = routing
        self.stealth_browser = stealth_browser
        self._empty_log = None  # lazy: EmptyLog(data/antibot/empty_slugs.json)

    @property
    def empty_log(self):
        """Lazy EmptyLog for marketplace 'empty since' bookkeeping."""
        if self._empty_log is None:
            from src.sources.marketplace.empty_log import EmptyLog

            self._empty_log = EmptyLog()
        return self._empty_log

    def _filter_backoff(self, adapter, tasks: list) -> list:
        """Drop marketplace tasks whose slug is in empty-since backoff.

        A slug that has rendered zero reviews for >= 3 consecutive cycles is
        skipped ("skipping (empty since X)") to conserve anti-bot budget.
        """
        if not str(getattr(adapter, "key", "")).startswith("marketplace_"):
            return tasks
        source = str(getattr(adapter, "key", ""))[len("marketplace_"):] or "g2"
        out: list = []
        for t in tasks:
            slug = (t.meta or {}).get("product_slug")
            if slug and self.empty_log.is_in_backoff(str(slug), source):
                entry = self.empty_log.entry(str(slug), source)
                logger.info(
                    "skipping {} (empty since {})", slug, entry.get("first_empty")
                )
                continue
            out.append(t)
        return out

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
            # Per-source site config: 'marketplace_<site>' adapters read
            # sites.<site> from the marketplace YAML (session cookies, etc.).
            for adapter in adapters:
                if adapter.key.startswith("marketplace_"):
                    site = adapter.key[len("marketplace_"):]
                    try:
                        site_cfg = self.config.load_yaml("marketplace").get("sites", {}).get(site, {})
                        cookie_file = site_cfg.get("session_cookie_file")
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
                    try:
                        self._run_fanout(adapter, eligible, stats, force=force, dry_run=dry_run, now=now)
                    except Exception as exc:
                        logger.exception("fanout adapter {} failed", adapter.key)
                        self._record_fail(adapter.key, "global", exc, cadence_hours=getattr(adapter, "cadence_hours", 24))
                        stats.failed += 1
                        stats._src(adapter.key)["failed"] += 1
                        self.ctx.bump(errors=1)
                    continue
                for account in eligible:
                    try:
                        self._run_pair(adapter, account, stats, force=force, dry_run=dry_run, max_passes=max_passes, limit_per_source=limit_per_source, now=now)
                    except Exception as exc:
                        logger.exception("adapter {} failed for {}", adapter.key, account.domain)
                        self._record_fail(adapter.key, account.domain, exc, cadence_hours=getattr(adapter, "cadence_hours", 24))
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
        # Per-domain open-role counts for the hiring-velocity trend signal,
        # accumulated across ALL pagination passes: every page's jobs land
        # here unconditionally so the final snapshot is the FULL cycle
        # count, not total-minus-page-1. The trend COMPUTE step below is
        # gated on no-follow-remaining instead of the accumulation.
        job_harvests: dict[str, list] = {}
        # ALL jobs harvested this cycle (every pass, every task). ATS
        # persistence (upsert + mark_closed + snapshot) is deferred to ONE
        # call after the pass loop using this complete set: marking closed
        # per page/task would close earlier pages' jobs (each call only
        # knows its own keys) — a pre-existing paginated-ATS bug that this
        # accumulation also fixes.
        cycle_jobs: list = []
        # Per-source pagination budget: sites.<site>.max_review_pages drives
        # how many follow passes the runner allows for this marketplace source
        # (g2 default 5, capterra default 3 -> max_passes = pages + 1).
        if adapter.key.startswith("marketplace_"):
            site = adapter.key[len("marketplace_"):]
            site_defaults = {"g2": 5, "capterra": 3, "trustradius": 2}
            pages_default = site_defaults.get(site, 5)
            try:
                site_cfg = self.config.load_yaml("marketplace").get("sites", {}).get(site, {})
                max_passes = int(site_cfg.get("max_review_pages", pages_default)) + 1
            except Exception:
                max_passes = pages_default + 1
        for pass_i in range(max_passes):
            if pending_follow:
                tasks = pending_follow
                pending_follow = []
            else:
                tasks = self._filter_backoff(adapter, adapter.plan(account, cursor))
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
            review_harvests: dict[str, list] = {}  # slug -> page-1 reviews
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
                    exc = RuntimeError(result.error or f"fetch failed {result.status}")
                    _attach_fetch_context(exc, result)
                    raise exc
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
                if adapter.key == "wayback" and (task.meta or {}).get("kind") == "pricing":
                    # Pricing change detection needs the PREVIOUS pricing
                    # snapshot for this domain (different doc_id, earlier
                    # fetch) to diff against. The adapter surface cannot
                    # reach the rawstore, so the runner injects it here —
                    # mirrors the federal_register watches pattern. No prior
                    # snapshot -> meta stays absent -> adapter conservatively
                    # emits nothing.
                    try:
                        meta["prev_pricing_html"] = self._prev_pricing_html(
                            task.domain, exclude_doc_id=result.doc.doc_id
                        )
                    except Exception:
                        logger.exception("prev pricing lookup failed for {}", task.domain)
                # Meta defaults are per marketplace site. The Capterra adapter
                # injects its own defaults (review_lookback_days=90,
                # max_review_pages=3) in plan(), so setdefault never overrides
                # them — this branch only fills gaps from sites.<site> config.
                if adapter.key.startswith("marketplace_"):
                    site = adapter.key[len("marketplace_"):]
                    site_meta_defaults = {"g2": (90, 5), "capterra": (90, 3), "trustradius": (90, 2)}
                    lookback_d, pages_d = site_meta_defaults.get(site, (90, 5))
                    try:
                        site_cfg = self.config.load_yaml("marketplace").get("sites", {}).get(site, {})
                        meta.setdefault("review_lookback_days", site_cfg.get("review_lookback_days", lookback_d))
                        meta.setdefault("max_review_pages", site_cfg.get("max_review_pages", pages_d))
                        meta.setdefault("click_show_more", site_cfg.get("deep_reviews", False))
                        # Deep-reviews bound: only injected when Show More
                        # expansion is on, so parse() can apply the extreme-
                        # rating filter at the review-filter level. Explicit
                        # null config value is preserved (legacy bound-free).
                        if site_cfg.get("deep_reviews", False):
                            meta.setdefault("deep_reviews_bound", site_cfg.get("deep_reviews_bound", "extreme"))
                    except Exception:
                        meta.setdefault("review_lookback_days", lookback_d)
                cands = adapter.parse(result.doc, account, meta)
                all_cands.extend(cands)
                follow.extend(adapter.follow_tasks(result.doc, account, meta) or [])
                jobs = adapter.harvest_jobs(result.doc, account, meta) or []
                # Accumulate EVERY page's jobs for the hiring-velocity trend
                # signal — pagination passes are additive, so the snapshot
                # after the final pass is the full open-role count. ATS
                # persistence is deferred to the single end-of-cycle call
                # below (mark_closed must see the complete key set).
                if jobs:
                    job_harvests.setdefault(account.domain, []).extend(jobs)
                    cycle_jobs.extend(jobs)
                harvest = getattr(adapter, "harvest_tech", None)
                if callable(harvest):
                    tech_harvests.append(harvest(result.doc, account, meta) or [])
                harvest_revs = getattr(adapter, "harvest_reviews", None)
                if callable(harvest_revs):
                    from src.sources.marketplace.collector import (
                        upsert_capterra_reviews,
                        upsert_g2_reviews,
                        upsert_trustradius_reviews,
                    )
                    revs = harvest_revs(result.doc, account, meta) or []
                    # Empty-since bookkeeping parity with the G2 fragment path
                    # (_fetch_g2_fragment): _filter_backoff applies empty-since
                    # backoff to EVERY marketplace_* source, but only G2 fed
                    # the log — capterra/trustradius slugs could never enter or
                    # reset backoff. Record their page results here exactly
                    # like the G2 branch does (zero reviews -> empty, reviews
                    # -> reset). Single-process assumption: record_* is a
                    # read-modify-write on the shared empty_slugs.json, so it
                    # is serialized via the fail-open state lock.
                    if (
                        adapter.key.startswith("marketplace_")
                        and adapter.key != "marketplace_g2"
                    ):
                        mslug = str(meta.get("product_slug") or "")
                        if mslug:
                            msource = adapter.key[len("marketplace_"):]
                            try:
                                with exclusive_lock(self.empty_log.path):
                                    if revs:
                                        self.empty_log.record_reviews(mslug, msource)
                                    else:
                                        self.empty_log.record_empty(mslug, msource)
                            except Exception:  # pragma: no cover - defensive
                                pass
                    if revs:
                        # Dispatch per adapter: the shared g2_reviews table
                        # stores provenance in its ``source`` column.
                        if adapter.key == "marketplace_capterra":
                            upsert_capterra_reviews(self.db, revs, now=_iso(now),
                                                    raw_ref=result.doc.doc_id)
                        elif adapter.key == "marketplace_trustradius":
                            upsert_trustradius_reviews(self.db, revs, now=_iso(now),
                                                       raw_ref=result.doc.doc_id)
                        elif adapter.key == "appstore_reviews":
                            # App Store harvest_reviews yields plain dicts,
                            # not attribute objects — must NOT fall through
                            # to upsert_g2_reviews (AttributeError).
                            from src.sources.appstores.appstore import (
                                upsert_appstore_reviews,
                            )
                            upsert_appstore_reviews(self.db, revs, now=_iso(now),
                                                    raw_ref=result.doc.doc_id)
                        else:
                            upsert_g2_reviews(self.db, revs, now=_iso(now),
                                              raw_ref=result.doc.doc_id)
                        # Track page-1 review stats per slug for the
                        # review-velocity trend signal (page 1 is the
                        # freshest page; consistent cycle-over-cycle).
                        if int(meta.get("page", 1) or 1) <= 1:
                            slug = str(meta.get("product_slug") or "")
                            if slug:
                                review_harvests.setdefault(slug, []).extend(revs)
            # Review-velocity trend signals: diff this cycle's per-slug
            # count/avg-rating against the previous cycle's stored stats.
            if review_harvests:
                from src.sources.marketplace.trend import (
                    DEFAULT_STATS_PATH as MARKETPLACE_STATS_PATH,
                    compute_stats,
                    load_stats,
                    review_trend_signal,
                    save_stats,
                    stats_key,
                )

                try:
                    rt_cfg = (self.config.load_yaml("marketplace") or {}).get(
                        "review_trend", {}
                    )
                except Exception:  # pragma: no cover - defensive
                    rt_cfg = {}
                source_name = adapter.key[len("marketplace_"):]
                # Single-process assumption: the load→mutate→save cycle below
                # touches a shared JSON file, so it is serialized against a
                # manually running `collect` via the fail-open state lock.
                with exclusive_lock(MARKETPLACE_STATS_PATH):
                    stats_state = load_stats()
                    for slug, revs in review_harvests.items():
                        curr = compute_stats(revs)
                        key = stats_key(source_name, slug)
                        cand = review_trend_signal(
                            slug,
                            source_name,
                            stats_state.get(key),
                            curr,
                            domain=account.domain,
                            today=now.date().isoformat(),
                            min_count_delta=int(rt_cfg.get("min_count_delta", 5)),
                            min_rating_delta=float(rt_cfg.get("min_rating_delta", 0.5)),
                        )
                        stats_state[key] = curr
                        if cand is not None:
                            all_cands.append(cand)
                    save_stats(stats_state)
            # Hiring-velocity trend signals: diff this cycle's per-domain
            # open-role count against the previous cycle's stored stats.
            # Only compute when pagination is FINISHED (no follow tasks
            # remain) — mid-paging snapshots would under-count, but the
            # accumulation above already captured every page's jobs.
            if job_harvests and not follow:
                from src.sources.jobsignals.trend import (
                    DEFAULT_STATS_PATH as JOBSIGNALS_STATS_PATH,
                    compute_stats,
                    hiring_trend_signal,
                    load_stats,
                    save_stats,
                    stats_key,
                )

                try:
                    ht_cfg = (self.config.load_yaml("jobsignals") or {}).get(
                        "hiring_trend", {}
                    )
                except Exception:  # pragma: no cover - defensive
                    ht_cfg = {}
                # Single-process assumption: the load→mutate→save cycle below
                # touches a shared JSON file, so it is serialized against a
                # manually running `collect` via the fail-open state lock.
                with exclusive_lock(JOBSIGNALS_STATS_PATH):
                    stats_state = load_stats()
                    for domain, jobs in job_harvests.items():
                        curr = compute_stats(jobs)
                        key = stats_key(domain)
                        cand = hiring_trend_signal(
                            domain,
                            stats_state.get(key),
                            curr,
                            today=now.date().isoformat(),
                            min_delta_pct=float(ht_cfg.get("min_delta_pct", 25.0)),
                            min_count=int(ht_cfg.get("min_count", DEFAULT_MIN_HIRING_COUNT)),
                        )
                        stats_state[key] = curr
                        if cand is not None:
                            all_cands.append(cand)
                    save_stats(stats_state)
            if tech_harvests:
                from src.sources.techstack.collector import upsert_technologies
                from src.sources.techstack.diff import diff_technologies
                from src.sources.techstack.fingerprint import load_fingerprint_rules, merge_matches

                merged = merge_matches(*tech_harvests)
                # Previous stored rows for this domain: the diff needs the
                # vendor SET; the renewal estimator also needs each vendor's
                # stored first_seen_at. Read BEFORE upsert_technologies so
                # first-seen history (not this cycle's fetch time) feeds it.
                try:
                    prev_rows = self.db.query(
                        "SELECT vendor, first_seen_at FROM technologies WHERE domain=?", (account.domain,)
                    )
                except Exception:
                    logger.exception("technologies lookup failed for {}", account.domain)
                    prev_rows = []
                # Diff against the previous cycle's vendor set for this domain
                # and persist any install/churn change signals before upserting.
                try:
                    previous = {r["vendor"] for r in prev_rows}
                    tech_changes = diff_technologies(
                        previous, {m.vendor for m in merged},
                        domain=account.domain, today=_iso(now)[:10],
                    )
                    change_cands = [cand for _, cand in tech_changes]
                except Exception:
                    logger.exception(
                        "techstack diff failed for {}", account.domain
                    )
                    change_cands = []
                if change_cands:
                    added = self._persist(account, adapter.key, change_cands, None)
                    stats.signals_new += added
                    stats._src(adapter.key)["signals_new"] += added
                    stats.candidates += len(change_cands)
                # Renewal-window estimation from the stored first-seen dates,
                # with per-vendor contract terms from fingerprints.yaml
                # (optional contract_years; default 1-year cycle). Same
                # fail-open pattern as the diff: estimation problems must
                # never block the harvest.
                try:
                    renewal_cands = _renewal_cands(
                        account.domain,
                        [
                            {"vendor": r["vendor"], "first_seen_at": r["first_seen_at"]}
                            for r in prev_rows
                        ],
                        now.date(),
                        load_fingerprint_rules(),
                    )
                except Exception:
                    logger.exception(
                        "renewal window estimation failed for {}", account.domain
                    )
                    renewal_cands = []
                if renewal_cands:
                    added = self._persist(account, adapter.key, renewal_cands, None)
                    stats.signals_new += added
                    stats._src(adapter.key)["signals_new"] += added
                    stats.candidates += len(renewal_cands)

                upsert_technologies(self.db, account.domain, merged, now=_iso(now))
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
        # End-of-cycle ATS persistence: ONE upsert with the COMPLETE cycle
        # job set, then mark_closed + snapshot. Doing this per page/task
        # would close earlier pages' jobs (each call only sees its own keys).
        if cycle_jobs:
            self._persist_jobs(adapter, account, cycle_jobs, now, more_pages=False)
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
                exc = RuntimeError(result.error or "fanout fetch failed")
                _attach_fetch_context(exc, result)
                # Stamp the cursor BEFORE the raise propagates: a fanout fetch
                # failure escapes run()'s per-account try/except (watch.py's
                # adapter-level catch handles it), so without this stamp the
                # cursor would never record fail_count / error_class /
                # backoff — the fanout analogue of the non-fanout path.
                self._record_fail(adapter.key, "global", exc, cadence_hours=getattr(adapter, "cadence_hours", 24))
                raise exc
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
        # marketplace_g2 ONLY: G2 serves reviews as client-rendered elv-* DOM in the
        # /products/{slug}/reviews_and_filters fragment (the /reviews page is
        # only the app shell). Prefer the rendered fragment via the DataDome
        # stealth browser so the Document body contains the real reviews.
        # NOTE: marketplace_capterra is deliberately NOT routed here — Capterra
        # is server-rendered (Next.js RSC; all review cards are in the raw
        # HTML of /reviews/) and CF-only, so it flows through the normal
        # fetcher.get path below. No fragment fetch (and no headed stealth
        # browser) is needed for Capterra.
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
        # The behavioral warm-up (~4s of homepage dwell = extra DataDome
        # exposure) only pays for itself on the first page of a session;
        # pagination pages 2..N reuse the already-warm session.
        try:
            page_num = int(page) if page else 1
        except (TypeError, ValueError):  # pragma: no cover - defensive
            page_num = 1
        # RouteState: if the domain's cookies are known-stale, the browser run
        # is still the solve for G2 — but skip the wasted warm-up latency.
        skip_warmup = False
        routing = getattr(self, "routing", None)
        if routing is not None:
            try:
                if routing.decide("g2.com") == "SkipToSolve":
                    skip_warmup = True
                    logger.info("skipping warm-up (cookies known-stale) for {}", task.domain)
            except Exception:  # pragma: no cover - defensive
                pass
        warmup_kwargs = (
            {"warmup_url": "https://www.g2.com/", "warmup_ms": 4000}
            if page_num <= 1 and not skip_warmup
            else {}
        )
        try:
            result = browser.fetch(
                url, source=task.source, domain=task.domain,
                wait_ms=4000, scroll=True, **warmup_kwargs,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("g2 fragment fetch failed for {}: {}", task.domain, exc)
            return None
        if result is not None and result.ok and result.doc is not None and result.doc.body:
            # Classify the fragment: DataDome challenge / rendered reviews / empty page.
            from src.sources.techstack.datadome import is_datadome_challenge
            from src.sources.marketplace.g2 import extract_g2_reviews

            body = result.doc.body
            if is_datadome_challenge(status=result.status, body=body):
                result.doc.g2_state = "challenge"
                logger.warning(
                    "g2 fragment for {} (slug={}) is a DataDome challenge — session "
                    "cookies likely stale; re-export data/g2_cookies.json from a "
                    "logged-in browser",
                    task.domain, slug,
                )
                if routing is not None:
                    try:
                        routing.expire("g2.com")
                    except Exception:  # pragma: no cover - defensive
                        pass
                return None  # fall back to the challenge->bypass->curl waterfall
            reviews_count = len(extract_g2_reviews(body.decode("utf-8", "replace"), slug))
            result.doc.g2_state = "ok" if reviews_count else "empty"
            # Empty-since bookkeeping: consecutive empty cycles put the slug
            # into cadence backoff; a cycle with reviews resets the counter.
            # Single-process assumption: record_* is a read-modify-write on
            # the shared empty_slugs.json — serialize it against a manually
            # running `collect` via the fail-open state lock.
            try:
                with exclusive_lock(self.empty_log.path):
                    if result.doc.g2_state == "empty":
                        self.empty_log.record_empty(str(slug), "g2")
                    else:
                        self.empty_log.record_reviews(str(slug), "g2")
            except Exception:  # pragma: no cover - defensive
                pass
            if reviews_count == 0:
                logger.info("g2 slug {} has zero reviews (new/quiet product) — not a block", slug)
            if routing is not None:
                try:
                    routing.record_solve("g2.com", cookies=["datadome"])
                except Exception:  # pragma: no cover - defensive
                    pass
            return result
        return None

    def _prev_pricing_html(self, domain: str, *, exclude_doc_id: str) -> Optional[str]:
        """Most recent wayback pricing snapshot body for this domain, EXCLUDING
        the doc currently being parsed (that's the 'current' side of the diff).

        Looks up prior pricing docs in the documents table (kind='pricing'
        tasks fetch a /pricing snapshot URL) and reads the body through the
        rawstore. Returns None when no prior snapshot exists — the caller
        then skips the diff (never fabricate a comparison).
        """
        rows = self.db.query(
            """
            SELECT doc_id, url FROM documents
            WHERE source = 'wayback' AND domain = ? AND doc_id != ?
              AND url LIKE '%/pricing%'
            ORDER BY fetched_at DESC
            LIMIT 1
            """,
            (domain, exclude_doc_id),
        )
        if not rows:
            return None
        doc = self.store.get(rows[0]["doc_id"])
        if doc is None or not doc.body:
            return None
        return doc.body.decode("utf-8", "replace")

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
                # Explicit clear: a recovered cursor must not keep the stale
                # error class of its previous failure state (COALESCE would
                # otherwise preserve it forever).
                "error_class": None,
            },
            pk=("source", "key"),
            overwrite={"fail_count", "last_error", "error_class"},
        )

    def _record_fail(self, source, key, exc, *, cadence_hours: int = 24):
        """Stamp the failure on the cursor: fail_count, error_class, and a
        next-due penalty multiplied by the class's BACKOFF_MULTIPLIER.

        The penalty is computed against the SOURCE's real cadence (passed by
        the call sites from ``adapter.cadence_hours``), not a global 24h: a
        336h-cadence source (wayback/crt.sh) compounds on its own scale and a
        12h ATS source takes 2x-shorter penalties.

        The TOTAL next-due penalty is capped scheduler-style at 8x the adapter
        cadence (mirrors scheduler.MAX_BACKOFF_EXPONENT = 3 → 2**3 == 8x), so
        compounding per-class penalties can never push next_due arbitrarily
        far out — and never double-book the scheduler's own capped backoff.
        """
        row = self._cursor(source, key) or {}
        n = int(row.get("fail_count") or 0) + 1
        status = getattr(exc, "fetch_status", None)
        error = getattr(exc, "fetch_error", None) or str(exc)
        body_hint = getattr(exc, "fetch_body_hint", None)
        error_class = classify_fetch_error(status, error, body_hint)
        multiplier = BACKOFF_MULTIPLIER.get(error_class, 1)
        cap = timedelta(hours=8 * cadence_hours)
        floor = _now()
        prev_due = row.get("next_due_at")
        if prev_due:
            penalty_days = (2 ** (n - 1)) * multiplier
            prev_dt = datetime.fromisoformat(prev_due)
            if prev_dt.tzinfo is None:
                prev_dt = prev_dt.replace(tzinfo=timezone.utc)
            due_dt = min(prev_dt + timedelta(days=penalty_days), floor + cap)
            due = _iso(due_dt)
        else:
            penalty_hours = cadence_hours * multiplier
            due = _iso(min(floor + timedelta(hours=penalty_hours), floor + cap))
        self.db.upsert(
            "source_cursors",
            {
                "source": source,
                "key": key,
                "cursor": row.get("cursor"),
                "etag": row.get("etag"),
                "last_modified": row.get("last_modified"),
                "last_run_at": row.get("last_run_at"),
                "next_due_at": due,
                "fail_count": n,
                "last_error": str(exc)[:500],
                "error_class": error_class.value,
            },
            pk=("source", "key"),
        )


def _renewal_cands(domain: str, tech_rows: list[dict], today, rules: dict) -> list:
    """Renewal-window candidates from stored technology rows.

    Per-vendor contract terms come from the fingerprints.yaml vendor specs
    (optional ``contract_years`` key, e.g. multi-year enterprise HCM deals);
    every other vendor assumes the 1-year default cycle. Pure computation
    (src.sources.wayback.renewal) — the caller wraps this in the techstack
    pass's fail-open try/except so estimation problems never block a harvest.
    """
    from src.sources.wayback.renewal import renewal_candidates

    vendors = (rules or {}).get("vendors") or {}
    contract_years = {
        name: int(spec["contract_years"])
        for name, spec in vendors.items()
        if isinstance(spec, dict) and spec.get("contract_years") is not None
    }
    return renewal_candidates(
        domain, tech_rows, contract_years=contract_years, default_years=1, today=today
    )


def _ckey(adapter, account) -> str:
    return "global" if getattr(adapter, "fanout", False) else account.domain


def _attach_fetch_context(exc: BaseException, result) -> None:
    """Attach fetch context to an exception so _record_fail can classify it.

    Carries the HTTP status, the error string, and (when a challenge body was
    stored) a decoded body hint for classify_fetch_error.
    """
    try:
        exc.fetch_status = result.status if getattr(result, "status", None) else None
        exc.fetch_error = getattr(result, "error", None)
        doc = getattr(result, "doc", None)
        body = getattr(doc, "body", None) if doc is not None else None
        if body:
            exc.fetch_body_hint = (
                body.decode("utf-8", "replace")
                if isinstance(body, (bytes, bytearray))
                else str(body)
            )
    except Exception:  # pragma: no cover - defensive
        pass


ATS_PREFIX = "ats_"
# Hiring-trend minimum absolute open-role count (MINOR b): a 3->4 jump is
# +33% but noise; below this floor no hiring_surge fires (config-overridable
# via jobsignals.hiring_trend.min_count).
DEFAULT_MIN_HIRING_COUNT = 5
_CF_BYPASS_SOURCES = {"techstack", "marketplace_g2", "marketplace_capterra", "marketplace_trustradius"}


def _ats_vendor_ok(adapter, account) -> bool:
    # Single source of truth: src/sources/registry.py owns the vendor gate
    # (COLLECTED_VENDORS + the ats_careers_page no-ATS special case).
    from src.sources.registry import _ats_vendor_ok as _registry_vendor_ok

    return _registry_vendor_ok(adapter, account)


def _requires_met(adapter, account) -> bool:
    for field_name in adapter.requires or ():
        if not getattr(account, field_name, None):
            return False
    return _ats_vendor_ok(adapter, account)
