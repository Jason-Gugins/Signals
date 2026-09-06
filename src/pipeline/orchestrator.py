"""Stage orchestrator: seed → resolve → collect → score → brief → export."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from loguru import logger

from src.core.config import Config
from src.core.db import Database
from src.core.models import Contact
from src.core.ratelimit import RateLimiter
from src.core.rawstore import RawStore
from src.core.runlog import RunContext
from src.export.briefs import render_brief, write_brief
from src.identity.icp import evaluate_icp
from src.identity.registry import AccountRegistry
from src.identity.seeds import SeedStats, seed_from_csv, seed_from_linkedin_db, seed_from_repvue_db
from src.pipeline.runner import CollectorRunner, RunnerStats
from src.signals.combos import evaluate_combos
from src.signals.lifecycle import load_supersede_map, partition_signals
from src.signals.normalize import normalize_batch
from src.signals.plays import assign_plays
from src.signals.calibration import load_stats
from src.signals.score import score_account
from src.signals.store import SignalStore
from src.signals.taxonomy import Taxonomy
from src.signals.tier import assign_tier
from src.sources.registry import SOURCES, enabled_sources


_INJECTED_TODAY: date | None = None


def set_today(d: date | None) -> None:
    """Inject a fixed 'today' for tests/reproducibility; None = real clock."""
    global _INJECTED_TODAY
    _INJECTED_TODAY = d


def _today() -> date:
    return _INJECTED_TODAY or date.today()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Orchestrator:
    def __init__(self, config: Config, *, db=None, fetcher=None, adapters=None):
        self.config = config
        Path(config.storage.raw_dir).mkdir(parents=True, exist_ok=True)
        Path(config.storage.briefs_dir).mkdir(parents=True, exist_ok=True)
        Path(config.storage.export_dir).mkdir(parents=True, exist_ok=True)
        Path(Path(config.storage.db_path).parent).mkdir(parents=True, exist_ok=True)
        self.db = db or Database(config.storage.db_path)
        self.registry = AccountRegistry(self.db)
        self.raw = RawStore(self.db, config.storage.raw_dir)
        self.taxonomy = Taxonomy.load()
        self.signal_store = SignalStore(self.db, self.taxonomy)
        self.fetcher = fetcher
        self._adapters = adapters

    def _signals_yaml(self) -> dict:
        """Load config/signals.yaml if present; missing file = no supersede windows."""
        try:
            return self.config.load_yaml("signals") or {}
        except FileNotFoundError:
            return {}

    def seed(self, *, csv: str | None = None, linkedin: bool = False, repvue: bool = False, cohort: str | None = None, limit: int | None = None) -> SeedStats:
        with RunContext(self.db, "seed") as ctx:
            stats = SeedStats()
            # Load ICP rules once so the seeder stays I/O-clean; {} keeps the
            # 1.0 default when config/icp.yaml is missing/empty.
            try:
                icp_rules = self.config.load_yaml("icp") or {}
            except Exception:
                icp_rules = {}
            if csv:
                part = seed_from_csv(self.registry, csv, cohort=cohort, icp_rules=icp_rules)
                stats.created += part.created
                stats.updated += part.updated
                stats.skipped += part.skipped
            if linkedin:
                part = seed_from_linkedin_db(self.registry, self.config.external_dbs.linkedin_db, cohort=cohort, limit=limit, icp_rules=icp_rules)
                stats.created += part.created
                stats.updated += part.updated
                stats.skipped += part.skipped
            if repvue:
                part = seed_from_repvue_db(self.registry, self.config.external_dbs.repvue_db, cohort=cohort, limit=limit, icp_rules=icp_rules)
                stats.created += part.created
                stats.updated += part.updated
                stats.skipped += part.skipped
            ctx.bump(accounts=stats.created)
            return stats

    def resolve(self, *, cohort=None, limit=None, domains: list[str] | None = None, ats: bool = True, cik: bool = True, feeds: bool = True, icp: bool = True, g2: bool = False, appstore: bool = False, bbb: bool = False, linkedin: bool = False) -> dict:
        with RunContext(self.db, "resolve") as ctx:
            if domains:
                accounts = self._accounts(domains=domains)
            else:
                accounts = self._accounts(cohort=cohort, limit=limit)
            ctx.bump(accounts=len(accounts))
            out = {"accounts": len(accounts), "cik": 0, "ats": 0, "feeds": 0, "icp": 0, "g2": 0, "appstore": 0, "bbb": 0, "linkedin": 0}
            if ats:
                from src.identity.ats_discovery import AtsDiscovery

                disc = AtsDiscovery(self.fetcher or self._http_fetcher(ctx), self.registry)
                for acct in accounts:
                    if acct.ats_token and acct.ats_vendor:
                        continue
                    try:
                        if disc.discover(acct):
                            out["ats"] += 1
                    except Exception as exc:
                        logger.warning("ats discover failed for {}: {}", acct.domain, exc)
            if cik:
                from src.identity.edgar_ids import EdgarIdentityResolver

                need = [a for a in accounts if not a.cik]
                if need:
                    try:
                        resolver = EdgarIdentityResolver(self.fetcher or self._http_fetcher(ctx), self.registry)
                        found = resolver.resolve_all(need)
                        out["cik"] = sum(1 for v in found.values() if v)
                    except Exception as exc:
                        logger.warning("cik resolve failed: {}", exc)
            if appstore:
                from src.identity.appstore_ids import AppStoreIdResolver

                need = [a for a in accounts if not a.app_store_id]
                if need:
                    try:
                        resolver = AppStoreIdResolver(self.fetcher or self._http_fetcher(ctx), self.registry)
                        found = resolver.resolve_all(need)
                        out["appstore"] = sum(1 for v in found.values() if v)
                    except Exception as exc:
                        logger.warning("appstore resolve failed: {}", exc)
            if bbb:
                from src.identity.bbb_ids import BbbProfileResolver

                need = [a for a in accounts if not (a.extra_data or {}).get("bbb_url")]
                if need:
                    try:
                        resolver = BbbProfileResolver(self.fetcher or self._http_fetcher(ctx), self.registry)
                        found = resolver.resolve_all(need)
                        out["bbb"] = sum(1 for v in found.values() if v)
                    except Exception as exc:
                        logger.warning("bbb resolve failed: {}", exc)
            if linkedin:
                from src.identity.linkedin_ids import LinkedinSlugResolver

                need = [a for a in accounts if not a.linkedin_slug]
                if need:
                    try:
                        resolver = LinkedinSlugResolver(self.config, self.registry)
                        found = resolver.resolve_all(need)
                        out["linkedin"] = sum(1 for v in found.values() if v)
                    except Exception as exc:
                        logger.warning("linkedin resolve failed: {}", exc)
            if feeds:
                from src.identity.feed_discovery import FeedDiscovery

                fdisc = FeedDiscovery(self.fetcher or self._http_fetcher(ctx), self.registry)
                for acct in accounts:
                    try:
                        if fdisc.discover(acct):
                            out["feeds"] += 1
                    except Exception as exc:
                        logger.warning("feed discover failed for {}: {}", acct.domain, exc)
            if icp:
                try:
                    rules = self.config.load_yaml("icp")
                except Exception:
                    rules = {}
                for acct in accounts:
                    try:
                        result = evaluate_icp(acct, rules, signals=self.signal_store.for_account(acct.domain))
                        acct.icp_fit = result.multiplier
                        acct.icp_reasons = result.reasons
                        acct.disqualified = result.disqualified
                        acct.disqualify_reason = result.disqualify_reason
                        self.registry.upsert(acct)
                        out["icp"] += 1
                    except Exception as exc:
                        logger.warning("icp failed for {}: {}", acct.domain, exc)
            if g2:
                from src.identity.g2_resolve import fetch_g2_search_url, parse_g2_search_results, resolve_g2_from_results

                fetcher = self.fetcher or self._http_fetcher(ctx)
                for acct in accounts:
                    if acct.g2_slug or not acct.name:
                        continue
                    try:
                        from src.identity.edgar_ids import _Task

                        url = fetch_g2_search_url(acct.name)
                        res = fetcher.get(_Task(source="g2_resolve", url=url, domain=acct.domain))
                        if not res or not res.ok or not res.doc or not res.doc.body:
                            continue
                        html = res.doc.body.decode("utf-8", "replace")
                        results = parse_g2_search_results(html)
                        resolved = resolve_g2_from_results(acct.name, results)
                        if resolved.status == "resolved" and resolved.slug:
                            acct.g2_slug = resolved.slug
                            self.registry.upsert(acct, source="g2_resolve")
                            out["g2"] += 1
                        elif resolved.candidates:
                            # Never-guess contract (mirrors capterra): ambiguity
                            # persists nothing and surfaces the candidate slugs.
                            logger.warning(
                                "g2 resolve not unique for {} (name={!r}): candidate slugs {}",
                                acct.domain,
                                acct.name,
                                [c.get("slug") for c in resolved.candidates],
                            )
                    except Exception as exc:
                        logger.warning("g2 resolve failed for {}: {}", acct.domain, exc)
            return out

    def discover(self, name: str, ddg: bool = False) -> dict:
        """Opt-in name->domain discovery waterfall (plan T4).

        Cross-stage logic lives in src/identity/discover.py; this method only
        supplies the shared HTTP fetcher + config and PERSISTS the outcome:
        the merged ranked candidates go into the identity_candidates review
        queue for every non-resolved run, and also for resolved runs that
        still produced alternate candidates (store everything human review
        might want). The resolved domain is returned for immediate use;
        account creation stays an explicit domain-keyed `sweep <domain>`.
        """
        with RunContext(self.db, "discover") as ctx:
            from src.identity.discover import discover_waterfall

            result = discover_waterfall(
                name,
                self.fetcher or self._http_fetcher(ctx),
                gkg_backend=getattr(self.config, "gkg_backend", "ekg"),
                ddg_enabled=ddg,
            )
            candidates = result.get("candidates") or []
            queued = result.get("status") != "resolved" or bool(candidates)
            if queued:
                from src.core.db import IdentityCandidateStore
                from src.identity.resolve import normalize_entity

                # Store key is the normalize_entity form (the table's
                # documented contract) so "Acme Inc" and "acme" land in one
                # review row; the raw name stays in the result for display.
                key = normalize_entity(name) or name
                IdentityCandidateStore(self.db).upsert_candidate(
                    key, "domain", candidates, source="discover"
                )
            result["queued"] = queued
            return result

    def discover_competitors(self, name: str) -> dict:
        """Keyless competitor-candidate mining into identity_candidates (T7).

        Carries T6's candidate-store mechanism per the T5 probe re-scope
        (data/probe/G2_COMPETITORS_2026_09.md): the G2 page pass is deferred
        (DataDome NO-GO), so ranked kind=competitor candidates come from Bing
        News RSS + HN Algolia co-mentions instead. Same contract as
        discover(): candidates are queued for human review only — activation
        stays an explicit human edit of config/lists/competitors.txt or
        config/fingerprints.yaml. Nothing here ever auto-creates or edits
        accounts.
        """
        with RunContext(self.db, "discover_competitors") as ctx:
            from src.identity.competitor_news import CompetitorNewsPass

            result = CompetitorNewsPass(
                self.fetcher or self._http_fetcher(ctx), self.registry
            ).discover(name)
            competitors = result.get("competitors") or []
            queued = bool(competitors)
            if queued:
                from src.core.db import IdentityCandidateStore
                from src.identity.resolve import normalize_entity

                # Store key is the normalize_entity form (the table's
                # documented contract), mirroring discover(): "Stripe Inc"
                # and "stripe" land in one review row; the raw name stays in
                # the result for display.
                key = normalize_entity(name) or name
                IdentityCandidateStore(self.db).upsert_candidate(
                    key, "competitor", competitors, source="competitor_news"
                )
            result["queued"] = queued
            result.setdefault("errors", {})
            return result

    def collect(self, *, sources=None, cohort=None, domains=None, force=False, dry_run=False, limit=None) -> RunnerStats:
        with RunContext(self.db, "collect") as ctx:
            accounts = self._accounts(cohort=cohort, domains=domains, limit=limit)
            adapters = self._pick_adapters(sources)
            cookie_jar = self._cookie_jar("http")
            fetcher = self.fetcher or self._http_fetcher(ctx)
            if cookie_jar is not None and getattr(fetcher, "cookie_jar", "missing") == "missing":
                try:
                    fetcher.cookie_jar = cookie_jar
                except Exception:
                    pass
            jars = [cookie_jar]
            stats = RunnerStats()
            formd = [a for a in adapters if a.key == "sec_formd"]
            if formd:
                now = datetime.now(timezone.utc).replace(microsecond=0)
                due_row = self.db.one(
                    "SELECT * FROM source_cursors WHERE source=? AND key=?",
                    ("sec_formd", "global"),
                )
                skip = (
                    not force
                    and due_row
                    and due_row.get("next_due_at")
                    and due_row["next_due_at"] > now.isoformat()
                )
                if skip:
                    stats.skipped += 1
                else:
                    from src.pipeline.funding import FundingTracker
                    from src.sources.sec.formd_filter import FormDFilter

                    table = self.config.load_yaml("sources")
                    entry = (table.get("sources") or {}).get("sec_formd") or {}
                    filt = FormDFilter(
                        include_funds=not entry.get("exclude_pooled_funds", True),
                        include_amendments=bool(entry.get("include_amendments", False)),
                    )
                    days = int(entry.get("recent_days") or 30)
                    tracker = FundingTracker(
                        self.config, self.db, self.registry, self.raw, fetcher, self.signal_store, self.taxonomy
                    )
                    tstats = tracker.run(
                        "recent",
                        today=date.today(),
                        days=days,
                        filt=filt,
                        persist=not dry_run,
                        dry_run=dry_run,
                        force=force,
                    )
                    stats.fetched += tstats.fetched
                    stats.signals_new += tstats.signals_new
                    hours = int(getattr(formd[0], "cadence_hours", None) or entry.get("cadence_hours") or 24)
                    self.db.upsert(
                        "source_cursors",
                        {
                            "source": "sec_formd",
                            "key": "global",
                            "cursor": None,
                            "last_run_at": now.isoformat(),
                            "next_due_at": (now + timedelta(hours=hours)).isoformat(),
                            "fail_count": 0,
                            "last_error": None,
                        },
                        pk=("source", "key"),
                    )
                adapters = [a for a in adapters if a.key != "sec_formd"]
            browser = None
            if self.config.browser.enabled:
                from src.core.browser import BrowserFetcher

                browser = BrowserFetcher(self.config, self.raw, ctx).start()
            cf_bypass = None
            if (
                getattr(self.config, "cloudflare", None)
                and self.config.cloudflare.enabled
                and browser is not None
            ):
                from src.core.db import CfCookieStore
                from src.sources.techstack.cf_bypass import CloudflareBypass

                cf_store = CfCookieStore(self.db)
                cf_bypass = CloudflareBypass(self.config, cf_store, fetcher, browser)
            dd_bypass = None
            stealth_browser = None
            routing = None
            if (
                getattr(self.config, "datadome", None)
                and self.config.datadome.enabled
            ):
                from src.core.curl_fetcher import CurlCffiFetcher
                from src.core.db import DataDomeCookieStore
                from src.sources.techstack.datadome_bypass import DataDomeBypass

                dd_store = DataDomeCookieStore(self.db)
                curl_fetcher = CurlCffiFetcher(
                    user_agent=self.config.browser.user_agent,
                    proxy=getattr(self.config.datadome, "residential_proxy", None),
                )
                # Only create the stealth browser if DataDome is enabled
                stealth_browser = None
                try:
                    from src.core.patchright_browser import PatchrightBrowserFetcher

                    stealth_browser = PatchrightBrowserFetcher(self.config, self.raw)
                except Exception:
                    pass  # patchright not installed — stealth tier disabled
                dd_bypass = DataDomeBypass(
                    self.config, dd_store, curl_fetcher,
                    stealth_browser=stealth_browser,
                )
            # SignalsShadow tier-1 (real Chrome TLS, antibot module) — wired into
            # both waterfalls when enabled. Guarded: the native engine may not
            # be built; waterfalls degrade to their existing tiers.
            if getattr(self.config, "antibot", None) and self.config.antibot.enabled:
                shadow = None
                try:
                    from src.antibot.python.transport import SignalsTransport

                    proxy = getattr(self.config.datadome, "residential_proxy", None)
                    shadow = SignalsTransport(
                        user_agent=self.config.browser.user_agent, proxy=proxy,
                    )
                except Exception:
                    shadow = None  # engine not built / import failed — tiers unchanged
                if shadow is not None:
                    if dd_bypass is not None:
                        dd_bypass.shadow = shadow
                    if cf_bypass is not None:
                        cf_bypass.shadow = shadow
                # RouteState: per-run cookie-lifetime learning (guarded).
                routing = None
                try:
                    from src.antibot.python.routing import RouteState

                    routing = RouteState()  # data/antibot/routing.json
                except Exception:
                    routing = None
            try:
                runner = CollectorRunner(
                    self.config, self.db, self.registry, self.raw, fetcher, self.signal_store, self.taxonomy, ctx,
                    browser=browser,
                    cloudflare_bypass=cf_bypass,
                    datadome_bypass=dd_bypass,
                    stealth_browser=stealth_browser,
                    routing=routing,
                )
                rest = runner.run(adapters, accounts, force=force, dry_run=dry_run)
            finally:
                if browser is not None:
                    browser.close()
                self._save_cookie_jars(jars)
            stats.fetched += rest.fetched
            stats.signals_new += rest.signals_new
            stats.failed += rest.failed
            stats.skipped += rest.skipped
            stats.cached += rest.cached
            stats.candidates += rest.candidates
            ctx.bump(accounts=len(accounts), signals_new=stats.signals_new)
            return stats

    def reparse(self, *, sources=None, since=None, domains=None) -> RunnerStats:
        with RunContext(self.db, "reparse") as ctx:
            stats = RunnerStats()
            wanted = set(sources) if sources else None
            adapters = {a.key: a for a in self._pick_adapters(None)}
            for row in self.db.query("SELECT doc_id, source, domain FROM documents"):
                if wanted and row["source"] not in wanted:
                    continue
                if domains and row["domain"] not in domains:
                    continue
                if since and row.get("fetched_at") and row["fetched_at"] < since:
                    continue
                adapter = adapters.get(row["source"])
                if adapter is None:
                    cls = SOURCES.get(row["source"])
                    adapter = cls() if cls else None
                if adapter is None:
                    continue
                doc = self.raw.get(row["doc_id"])
                if doc is None:
                    continue
                account = self.registry.get(doc.domain) if doc.domain else None
                if account is None:
                    continue
                try:
                    meta = {
                        "today": _today().isoformat(),
                        "registry": self.registry,
                    }
                    if adapter.key == "sec_edgar":
                        url = (doc.url or "").lower()
                        if "primary_doc.xml" in url or url.endswith(".xml"):
                            meta["kind"] = "form_d"
                        elif "8-k" in url or url.endswith(".htm") or url.endswith(".html"):
                            meta["kind"] = "8k"
                        else:
                            meta["kind"] = "submissions"
                    if adapter.key == "sec_formd":
                        url = (doc.url or "").lower()
                        if url.endswith(".xml") or "primary_doc.xml" in url:
                            meta["kind"] = "form_d"
                        elif "search-index" in url:
                            meta["kind"] = "fts"
                        else:
                            meta["kind"] = "submissions"
                    if adapter.key == "federal_register":
                        try:
                            meta["watches"] = (self.config.load_yaml("regulations").get("watches") or [])
                        except Exception:
                            meta["watches"] = []
                    if adapter.key == "company_feed":
                        meta.setdefault("kind", "blog")
                    cands = adapter.parse(doc, account, meta)
                except Exception as exc:
                    logger.warning("reparse {} {}: {}", row["source"], row["doc_id"], exc)
                    stats.failed += 1
                    continue
                stats.candidates += len(cands)
                valid, _ = normalize_batch(
                    cands, account=account, source=adapter.key, taxonomy=self.taxonomy, now=_now(), raw_ref=doc.doc_id
                )
                new, _ = self.signal_store.upsert_many(valid)
                stats.signals_new += new
                self.raw.mark_parsed(doc.doc_id)
            ctx.bump(signals_new=stats.signals_new)
            return stats

    def score(self, *, cohort=None, domains=None) -> dict:
        with RunContext(self.db, "score") as ctx:
            scoring = self.config.load_yaml("scoring")
            plays_cfg = self.config.load_yaml("plays")
            supersede_map = load_supersede_map(self._signals_yaml())
            calibration_stats = load_stats(self.db)
            today = _today()
            n = 0
            for acct in self._accounts(cohort=cohort, domains=domains):
                signals = self.signal_store.for_account(acct.domain)
                # Soft-filter superseded signals: expired ones stay in the DB untouched,
                # but only active signals contribute to combos/score/tier/plays.
                signals, _expired = partition_signals(
                    signals, today=today, supersede_days_by_type=supersede_map
                )
                combos = evaluate_combos(signals, scoring.get("combos") or [], today=today)
                result = score_account(acct, signals, taxonomy=self.taxonomy, cfg=scoring, today=today, combos=combos, calibration_stats=calibration_stats)
                tier = assign_tier(signals, result, taxonomy=self.taxonomy, cfg=scoring, today=today)
                contacts = self._contacts(acct.domain)
                plays = assign_plays(acct, signals, result, tier, taxonomy=self.taxonomy, plays_cfg=plays_cfg, contacts=contacts, today=today)
                as_of = today.isoformat()
                self.registry.set_scores(acct.domain, score=result.score, tier=tier.tier, buying_window=tier.buying_window, scored_at=as_of)
                self.db.upsert(
                    "score_history",
                    {
                        "domain": acct.domain,
                        "as_of": as_of,
                        "score": result.score,
                        "tier": tier.tier,
                        "buying_window": tier.buying_window,
                        "components": result.to_components_json(),
                    },
                    pk=("domain", "as_of"),
                )
                for play in plays:
                    self.db.upsert(
                        "play_assignments",
                        {
                            "domain": acct.domain,
                            "play_id": play.play_id,
                            "signal_id": play.signal_id or "",
                            "rank": play.rank,
                            "urgency": play.urgency,
                            "variables": None,
                            "opener": play.opener,
                            "t24": play.t24,
                            "generated_at": as_of,
                        },
                        pk=("domain", "play_id", "signal_id"),
                    )
                n += 1
            ctx.bump(accounts=n)
            return {"scored": n}

    def brief(self, *, cohort=None, domains=None, tier_max=2) -> list[str]:
        with RunContext(self.db, "brief") as ctx:
            scoring = self.config.load_yaml("scoring")
            plays_cfg = self.config.load_yaml("plays")
            calibration_stats = load_stats(self.db)
            today = _today()
            paths = []
            for acct in self._accounts(cohort=cohort, domains=domains):
                if acct.tier and acct.tier > tier_max:
                    continue
                signals = self.signal_store.for_account(acct.domain)
                combos = evaluate_combos(signals, scoring.get("combos") or [], today=today)
                result = score_account(acct, signals, taxonomy=self.taxonomy, cfg=scoring, today=today, combos=combos, calibration_stats=calibration_stats)
                tier = assign_tier(signals, result, taxonomy=self.taxonomy, cfg=scoring, today=today)
                contacts = self._contacts(acct.domain)
                plays = assign_plays(acct, signals, result, tier, taxonomy=self.taxonomy, plays_cfg=plays_cfg, contacts=contacts, today=today)
                text = render_brief(acct, signals, result, tier, plays, contacts, today=today)
                path = Path(self.config.storage.briefs_dir) / f"{acct.domain}.md"
                write_brief(str(path), text)
                paths.append(str(path))
            ctx.bump(accounts=len(paths))
            return paths

    def funding(
        self,
        mode,
        *,
        dry_run=False,
        q=None,
        cik=None,
        domain=None,
        days=30,
        include_funds=False,
        include_amendments=False,
        min_sold=0,
        state=None,
        limit=100,
    ):
        from src.pipeline.funding import FundingTracker
        from src.sources.sec.formd_filter import FormDFilter

        fetcher = self.fetcher or self._http_fetcher(None)
        tracker = FundingTracker(
            self.config, self.db, self.registry, self.raw, fetcher, self.signal_store, self.taxonomy
        )
        filt = FormDFilter(
            include_funds=include_funds,
            include_amendments=include_amendments,
            min_sold=min_sold or 0,
            state=state,
        )
        return tracker.run(
            mode,
            today=_today(),
            q=q,
            cik=cik,
            domain=domain,
            days=days,
            filt=filt,
            limit=limit,
            persist=not dry_run,
            dry_run=dry_run,
        )

    def export(self, *, cohort=None, fmt="csv", what="all") -> list[str]:
        with RunContext(self.db, "export"):
            try:
                from src.export.csvout import export_all, export_funding

                if what == "funding":
                    return [export_funding(self.db, str(Path(self.config.storage.export_dir) / "funding.csv"), cohort=cohort)]
                return export_all(self.db, self.config.storage.export_dir, cohort=cohort)
            except ImportError:
                return []

    def run_all(self, **kw) -> dict:
        strict = kw.pop("strict", False)
        skip_seed = kw.pop("skip_seed", False)
        skip_collect = kw.pop("skip_collect", False)
        out: dict = {"errors": {}}
        stages = []
        if not skip_seed:
            stages.append(("seed", lambda: self.seed(csv=kw.get("csv"), linkedin=kw.get("linkedin", False), repvue=kw.get("repvue", False), cohort=kw.get("cohort"))))
        stages.append(("resolve", lambda: self.resolve(cohort=kw.get("cohort"), limit=kw.get("limit"))))
        if not skip_collect:
            stages.append(("collect", lambda: self.collect(cohort=kw.get("cohort"), domains=kw.get("domains"), force=kw.get("force", False), dry_run=kw.get("dry_run", False), limit=kw.get("limit"))))
        stages.append(("score", lambda: self.score(cohort=kw.get("cohort"), domains=kw.get("domains"))))
        stages.append(("brief", lambda: self.brief(cohort=kw.get("cohort"), domains=kw.get("domains"), tier_max=kw.get("tier_max", 2))))
        stages.append(("export", lambda: self.export(cohort=kw.get("cohort"), fmt=kw.get("fmt", "csv"))))
        for name, fn in stages:
            try:
                out[name] = fn()
            except Exception as exc:
                logger.exception("stage {} failed", name)
                out[name] = None
                out["errors"][name] = str(exc)
                self._mark_failed(name)
                if strict:
                    raise
        return out

    def _mark_failed(self, stage: str) -> None:
        try:
            with RunContext(self.db, stage):
                raise RuntimeError("stage failed")
        except RuntimeError:
            pass

    def _accounts(self, *, cohort=None, domains=None, limit=None):
        if domains:
            found = []
            for d in domains:
                acct = self.registry.get(d)
                if acct:
                    found.append(acct)
            return found
        return self.registry.list_accounts(cohort=cohort, limit=limit)

    def _contacts(self, domain: str) -> list[Contact]:
        rows = self.db.query("SELECT * FROM contacts WHERE domain = ?", (domain,))
        return [Contact.from_db_row(r) for r in rows]

    def _pick_adapters(self, sources):
        if self._adapters is not None and not sources:
            return list(self._adapters)
        adapters = list(self._adapters or enabled_sources(self.config))
        if sources:
            wanted = set(sources)
            adapters = [a for a in adapters if a.key in wanted]
        return adapters

    def _source_rate_overrides(self) -> dict[str, float]:
        """Per-source ``rate_per_host`` overrides from config/sources.yaml.

        Only entries that define their own rate_per_host are included; sources
        without one keep the global default / per-host behavior unchanged.
        """
        try:
            table = self.config.load_yaml("sources") or {}
        except Exception:
            return {}
        entries = table.get("sources", table)
        out: dict[str, float] = {}
        for key, entry in (entries or {}).items():
            if not isinstance(entry, dict):
                continue
            rate = entry.get("rate_per_host")
            if rate is None:
                continue
            try:
                out[key] = float(rate)
            except (TypeError, ValueError):
                continue
        return out

    def _http_fetcher(self, ctx):
        from src.core.http import HttpFetcher

        return HttpFetcher(
            self.config,
            self.raw,
            RateLimiter(
                self.config.http.default_rate_per_host,
                per_source=self._source_rate_overrides(),
            ),
            ctx=ctx,
        )

    def _cookie_jar(self, scope: str):
        """Optional core cookie jar for the given transport scope, or None.

        Disabled by default: only constructed when ``config.cookies.enabled``
        is true (nothing sets it today, so behavior is unchanged). When
        enabled, the jar is loaded from its per-scope file up front so
        cookies survive across runs, and the caller saves it after collect.
        """
        if not getattr(getattr(self.config, "cookies", None), "enabled", False):
            return None
        try:
            from src.core.cookiejar import PersistentCookieJar

            jar = PersistentCookieJar(scope=scope)
            jar.load()
            return jar
        except Exception:
            return None

    def _save_cookie_jars(self, jars) -> None:
        """Best-effort persistence of any cookie jars created for this run."""
        for jar in jars:
            if jar is None:
                continue
            try:
                jar.save()
            except Exception:
                logger.exception("cookie jar save failed for scope={}", getattr(jar, "scope", "?"))
