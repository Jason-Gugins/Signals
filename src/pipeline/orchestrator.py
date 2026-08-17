"""Stage orchestrator: seed → resolve → collect → score → brief → export."""

from __future__ import annotations

from datetime import date, datetime, timezone
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
from src.signals.normalize import normalize_batch
from src.signals.plays import assign_plays
from src.signals.score import score_account
from src.signals.store import SignalStore
from src.signals.taxonomy import Taxonomy
from src.signals.tier import assign_tier
from src.sources.registry import SOURCES, enabled_sources


def _today() -> date:
    return date(2026, 8, 16)


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

    def seed(self, *, csv: str | None = None, linkedin: bool = False, repvue: bool = False, cohort: str | None = None, limit: int | None = None) -> SeedStats:
        with RunContext(self.db, "seed") as ctx:
            stats = SeedStats()
            if csv:
                part = seed_from_csv(self.registry, csv, cohort=cohort)
                stats.created += part.created
                stats.updated += part.updated
                stats.skipped += part.skipped
            if linkedin:
                part = seed_from_linkedin_db(self.registry, self.config.external_dbs.linkedin_db, cohort=cohort, limit=limit)
                stats.created += part.created
                stats.updated += part.updated
                stats.skipped += part.skipped
            if repvue:
                part = seed_from_repvue_db(self.registry, self.config.external_dbs.repvue_db, cohort=cohort, limit=limit)
                stats.created += part.created
                stats.updated += part.updated
                stats.skipped += part.skipped
            ctx.bump(accounts=stats.created)
            return stats

    def resolve(self, *, cohort=None, limit=None) -> dict:
        with RunContext(self.db, "resolve") as ctx:
            accounts = self._accounts(cohort=cohort, limit=limit)
            ctx.bump(accounts=len(accounts))
            # Network-backed resolvers degrade to no-ops when they fail.
            out = {"accounts": len(accounts), "cik": 0, "ats": 0, "feeds": 0, "icp": 0}
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
            return out

    def collect(self, *, sources=None, cohort=None, domains=None, force=False, dry_run=False, limit=None) -> RunnerStats:
        with RunContext(self.db, "collect") as ctx:
            accounts = self._accounts(cohort=cohort, domains=domains, limit=limit)
            adapters = self._pick_adapters(sources)
            fetcher = self.fetcher or self._http_fetcher(ctx)
            runner = CollectorRunner(
                self.config, self.db, self.registry, self.raw, fetcher, self.signal_store, self.taxonomy, ctx
            )
            stats = runner.run(adapters, accounts, force=force, dry_run=dry_run)
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
            today = _today()
            n = 0
            for acct in self._accounts(cohort=cohort, domains=domains):
                signals = self.signal_store.for_account(acct.domain)
                combos = evaluate_combos(signals, scoring.get("combos") or [], today=today)
                result = score_account(acct, signals, taxonomy=self.taxonomy, cfg=scoring, today=today, combos=combos)
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
            today = _today()
            paths = []
            for acct in self._accounts(cohort=cohort, domains=domains):
                if acct.tier and acct.tier > tier_max:
                    continue
                signals = self.signal_store.for_account(acct.domain)
                combos = evaluate_combos(signals, scoring.get("combos") or [], today=today)
                result = score_account(acct, signals, taxonomy=self.taxonomy, cfg=scoring, today=today, combos=combos)
                tier = assign_tier(signals, result, taxonomy=self.taxonomy, cfg=scoring, today=today)
                contacts = self._contacts(acct.domain)
                plays = assign_plays(acct, signals, result, tier, taxonomy=self.taxonomy, plays_cfg=plays_cfg, contacts=contacts, today=today)
                text = render_brief(acct, signals, result, tier, plays, contacts, today=today)
                path = Path(self.config.storage.briefs_dir) / f"{acct.domain}.md"
                write_brief(str(path), text)
                paths.append(str(path))
            ctx.bump(accounts=len(paths))
            return paths

    def export(self, *, cohort=None, fmt="csv") -> list[str]:
        with RunContext(self.db, "export"):
            try:
                from src.export.csvout import export_all

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

    def _http_fetcher(self, ctx):
        from src.core.http import HttpFetcher

        return HttpFetcher(self.config, self.raw, RateLimiter(self.config.http.default_rate_per_host), ctx=ctx)
