"""Signals CLI."""

from __future__ import annotations

import sys
from pathlib import Path

import click
from loguru import logger

from src.core.config import Config
from src.core.db import Database
from src.core.rawstore import RawStore
from src.identity.lists import load_champions, upsert_champions
from src.identity.registry import AccountRegistry
from src.pipeline.orchestrator import Orchestrator


def _dirs(cfg: Config) -> list[Path]:
    return [
        Path(cfg.storage.db_path).parent,
        Path(cfg.storage.raw_dir),
        Path(cfg.storage.export_dir),
        Path(cfg.storage.briefs_dir),
        Path(cfg.storage.alerts_dir),
        Path(cfg.storage.recon_dir),
        Path("data/logs"),
        Path("data/inbox/owned"),
        Path("config/lists"),
    ]


@click.group()
@click.option("--config", "config_path", default=None, help="Path to default.yaml")
@click.option("--cohort", default=None)
@click.option("-v", "--verbose", is_flag=True)
@click.option("--dry-run", is_flag=True)
@click.pass_context
def main(ctx, config_path, cohort, verbose, dry_run):
    ctx.ensure_object(dict)
    cfg = Config.load(config_path)
    if verbose:
        logger.remove()
        logger.add(sys.stderr, level="DEBUG")
    ctx.obj["config"] = cfg
    ctx.obj["cohort"] = cohort
    ctx.obj["dry_run"] = dry_run
    ctx.obj["orch"] = None

    def get_orch():
        if ctx.obj["orch"] is None:
            ctx.obj["orch"] = Orchestrator(cfg)
        return ctx.obj["orch"]

    ctx.obj["get_orch"] = get_orch


@main.command()
@click.pass_context
def init(ctx):
    cfg = ctx.obj["config"]
    for d in _dirs(cfg):
        d.mkdir(parents=True, exist_ok=True)
    Database(cfg.storage.db_path)
    lists = Path("config/lists")
    readme = lists / "README.md"
    if not readme.exists():
        readme.write_text("Drop champions.csv, exclusions.txt, email_patterns.csv here.\n", encoding="utf-8")
    click.echo("Initialized data/ and config/lists/. Next: signals seed --csv accounts.csv")


@main.command()
@click.option("--csv", "csv_path", default=None)
@click.option("--linkedin", is_flag=True)
@click.option("--repvue", is_flag=True)
@click.option("--limit", type=int, default=None)
@click.option("--cohort", default=None)
@click.pass_context
def seed(ctx, csv_path, linkedin, repvue, limit, cohort):
    orch: Orchestrator = ctx.obj["get_orch"]()
    stats = orch.seed(csv=csv_path, linkedin=linkedin, repvue=repvue, cohort=cohort or ctx.obj["cohort"], limit=limit)
    click.echo(f"created={stats.created} updated={stats.updated} skipped={stats.skipped}")


@main.command()
@click.option("--cik/--no-cik", default=True)
@click.option("--ats/--no-ats", default=True)
@click.option("--feeds/--no-feeds", default=True)
@click.option("--icp/--no-icp", default=True)
@click.option("--g2/--no-g2", default=False)
@click.option("--capterra/--no-capterra", default=False)
@click.option("--limit", type=int, default=None)
@click.pass_context
def resolve(ctx, cik, ats, feeds, icp, g2, capterra, limit):
    orch: Orchestrator = ctx.obj["get_orch"]()
    if capterra:
        _resolve_capterra(orch, cohort=ctx.obj["cohort"], limit=limit)
        return
    try:
        out = orch.resolve(cohort=ctx.obj["cohort"], limit=limit, ats=ats, cik=cik, feeds=feeds, icp=icp, g2=g2)
        click.echo(f"resolved accounts={out.get('accounts', 0)}")
    except Exception as exc:
        click.echo(f"resolve degraded: {exc}")


def _append_segment(existing: str | None, segment: str) -> str:
    """Append a Capterra segment to a comma-separated g2_slug value. PURE."""
    parts = [p.strip() for p in (existing or "").split(",") if p.strip()]
    if segment not in parts:
        parts.append(segment)
    return ",".join(parts)


def _resolve_capterra(orch: Orchestrator, cohort=None, limit=None) -> None:
    """Resolve Capterra segments for accounts and append them to g2_slug.

    Ambiguous matches list candidates and exit non-zero without writing.
    """
    from src.identity import capterra_resolve

    accounts = orch.registry.list_accounts(cohort=cohort, limit=limit, order_by="domain")
    ambiguous: list[tuple[str, list[dict]]] = []
    resolved = 0
    for acct in accounts:
        if not acct.name:
            continue
        result = capterra_resolve.resolve_capterra(acct.name)
        if result.status == "resolved" and result.segment:
            if result.segment in [p.strip() for p in (acct.g2_slug or "").split(",")]:
                continue
            acct.g2_slug = _append_segment(acct.g2_slug, result.segment)
            orch.registry.upsert(acct, source="capterra_resolve")
            click.echo(f"{acct.domain}: {result.segment}")
            resolved += 1
        elif result.status == "ambiguous":
            names = ", ".join(f"{c.get('segment')} ({c.get('name')})" for c in result.candidates)
            click.echo(f"{acct.domain}: ambiguous for {acct.name!r} — {names}", err=True)
            ambiguous.append((acct.domain, result.candidates))
        else:
            click.echo(f"{acct.domain}: no Capterra match for {acct.name!r}", err=True)
    click.echo(f"capterra resolved={resolved} ambiguous={len(ambiguous)}")
    if ambiguous:
        ctx_exit(1)


def ctx_exit(code):  # indirection keeps tests patchable
    sys.exit(code)


@main.command()
@click.option("--tier", type=int, default=None)
@click.option("--cohort", default=None)
@click.option("--limit", type=int, default=None)
@click.pass_context
def accounts(ctx, tier, cohort, limit):
    cfg = ctx.obj["config"]
    db = Database(cfg.storage.db_path)
    rows = AccountRegistry(db).list_accounts(cohort=cohort or ctx.obj["cohort"], tier=tier, limit=limit, order_by="domain")
    click.echo("domain\tname\ttier\tscore")
    for a in rows:
        click.echo(f"{a.domain}\t{a.name or ''}\t{a.tier or ''}\t{a.score or ''}")


@main.command()
@click.option("--load", "load_path", required=True)
@click.pass_context
def champions(ctx, load_path):
    db = Database(ctx.obj["config"].storage.db_path)
    n = upsert_champions(db, load_champions(load_path))
    click.echo(f"champions={n}")


@main.command()
@click.option("--source", "sources", multiple=True)
@click.option("--domain", "domains", multiple=True)
@click.option("--force", is_flag=True)
@click.option("--limit", type=int, default=None)
@click.pass_context
def collect(ctx, sources, domains, force, limit):
    orch: Orchestrator = ctx.obj["get_orch"]()
    stats = orch.collect(
        sources=list(sources) or None,
        domains=list(domains) or None,
        force=force,
        dry_run=ctx.obj["dry_run"],
        limit=limit,
        cohort=ctx.obj["cohort"],
    )
    click.echo(f"fetched={stats.fetched} signals_new={stats.signals_new} failed={stats.failed}")


@main.command()
@click.option("--source", "sources", multiple=True)
@click.option("--since", default=None)
@click.pass_context
def reparse(ctx, sources, since):
    stats = ctx.obj["get_orch"]().reparse(sources=list(sources) or None, since=since)
    click.echo(f"reparsed candidates={stats.candidates} new={stats.signals_new}")


@main.command()
@click.option("--domain", "domains", multiple=True)
@click.pass_context
def score(ctx, domains):
    out = ctx.obj["get_orch"]().score(domains=list(domains) or None, cohort=ctx.obj["cohort"])
    click.echo(f"scored={out.get('scored', 0)}")


@main.command()
@click.option("--domain", "domains", multiple=True)
@click.option("--tier-max", type=int, default=2)
@click.option("--open", "open_files", is_flag=True)
@click.pass_context
def brief(ctx, domains, tier_max, open_files):
    paths = ctx.obj["get_orch"]().brief(domains=list(domains) or None, cohort=ctx.obj["cohort"], tier_max=tier_max)
    for p in paths:
        click.echo(p)
    if open_files:
        click.echo("(open skipped)")


@main.command(name="export")
@click.option("--format", "fmt", type=click.Choice(["csv", "json"]), default="csv")
@click.option("--what", default="all")
@click.pass_context
def export_cmd(ctx, fmt, what):
    paths = ctx.obj["get_orch"]().export(cohort=ctx.obj["cohort"], fmt=fmt, what=what)
    for p in paths:
        click.echo(p)


@main.command(name="g2-export")
@click.option("--slug", "slugs", multiple=True, help="Product slug(s) to export")
@click.option("--dir", "export_dir", default=None, help="Export directory")
@click.option("--format", "fmt", type=click.Choice(["json", "csv", "both"]), default="both")
@click.pass_context
def g2_export(ctx, slugs, export_dir, fmt):
    """Export G2 reviews to JSON and/or CSV."""
    from src.export.g2_export import export_g2_json, export_g2_csv, export_g2_all
    cfg = ctx.obj["config"]
    db = Database(cfg.storage.db_path)
    out_dir = export_dir or cfg.storage.export_dir
    paths = []
    if slugs:
        for slug in slugs:
            if fmt in ("json", "both"):
                paths.append(export_g2_json(db, slug, out_dir))
            if fmt in ("csv", "both"):
                paths.append(export_g2_csv(db, slug, out_dir))
    else:
        paths = export_g2_all(db, out_dir)
    for p in paths:
        click.echo(p)


class _RawShim:
    """No-op raw store for self-check fetches (self-check does not persist)."""

    def put(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


@main.command()
@click.option("--keep-days", default=90, show_default=True, help="Delete rows/files older than N days.")
@click.option("--vacuum", is_flag=True, help="Also VACUUM the database to reclaim space.")
@click.pass_context
def prune(ctx, keep_days, vacuum):
    """Enforce data retention: fetch_log/documents/runs + raw store files."""
    from src.core.db import prune_all

    cfg = ctx.obj["config"]
    db = Database(cfg.storage.db_path)
    raw_store = RawStore(db, cfg.storage.raw_dir)
    counts = prune_all(db, keep_days=keep_days, raw_store=raw_store)
    if vacuum:
        db.conn.execute("VACUUM")
        db.conn.commit()
    click.echo(str(counts))


@main.command(name="g2-selfcheck")
@click.option("--slug", default="sierra", show_default=True, help="Known-good G2 product slug")
@click.option("--headless/--headed", "headless", default=None, help="Override browser.headless (G2 needs headed)")
@click.pass_context
def g2_selfcheck(ctx, slug, headless):
    """Live selector-drift self-check: fetch one G2 product and verify the parser still works."""
    from src.core.patchright_browser import PatchrightBrowserFetcher
    from src.sources.marketplace.selfcheck import run_selfcheck

    cfg = ctx.obj["config"]
    # G2/DataDome requires headed mode; force it unless explicitly overridden.
    if headless is None:
        headless = False
    cfg.browser.headless = headless
    fetcher = PatchrightBrowserFetcher(cfg, _RawShim())
    r = run_selfcheck(fetcher, slug=slug, config=cfg)
    click.echo(f"state={r.state} reviews={r.review_count} url={r.url} {r.detail}")
    if r.state in ("drift", "challenge", "error"):
        raise SystemExit(1)


@main.command(name="capterra-selfcheck")
@click.option("--slug", default="19319/JIRA", show_default=True,
              help="Capterra '<numeric-id>/<Slug>' segment (e.g. 19319/JIRA)")
@click.pass_context
def capterra_selfcheck(ctx, slug):
    """Capterra selector-drift self-check.

    Capterra's reviews pages are server-rendered, so this uses the plain
    CurlCffi HTTP fetcher — no stealth browser window. One paced request
    (~4s+ spacing recommended between live runs; Capterra is CF-fronted).
    """
    import time

    from src.core.curl_fetcher import CurlCffiFetcher
    from src.core.http import FetchResult
    from src.core.models import Document
    from src.sources.marketplace.selfcheck import run_selfcheck

    cfg = ctx.obj["config"]

    class _CurlShim:
        """Adapts CurlCffiFetcher.get -> fetch(url) -> FetchResult(Document)."""

        def __init__(self):
            dd = getattr(cfg, "datadome", None)
            self._curl = CurlCffiFetcher(
                user_agent=cfg.browser.user_agent,
                proxy=getattr(dd, "residential_proxy", None),
            )

        def fetch(self, url, **kwargs):
            t0 = time.monotonic()
            r = self._curl.get(url)
            doc = Document(doc_id=f"capterra-selfcheck-{int(t0 * 1000)}",
                           source="marketplace_capterra", url=url,
                           body=r.body, status=r.status)
            return FetchResult(ok=200 <= r.status < 400, status=r.status,
                               doc=doc, cached=False, error=None,
                               elapsed_ms=int((time.monotonic() - t0) * 1000))

    r = run_selfcheck(_CurlShim(), slug=slug, config=cfg, source="capterra")
    click.echo(f"state={r.state} reviews={r.review_count} url={r.url} {r.detail}")
    if r.state in ("drift", "challenge", "error"):
        raise SystemExit(1)


@main.group()
@click.pass_context
def funding(ctx):
    """Company Funding Tracker (SEC Form D)."""


def _run_funding(ctx, mode, **kwargs):
    orch = ctx.obj["get_orch"]()
    stats = orch.funding(mode, dry_run=ctx.obj["dry_run"], **kwargs)
    rows = getattr(stats, "rows", None) or []
    if rows:
        click.echo("observed_at\tsignal_type\ttitle\turl")
        for cand in rows:
            click.echo(f"{cand.observed_at}\t{cand.signal_type}\t{cand.title or ''}\t{cand.url or ''}")
    click.echo(f"fetched={stats.fetched} kept={stats.kept} signals_new={stats.signals_new} csv={stats.csv_path or ''}")


@funding.command("recent")
@click.option("--days", type=int, default=30)
@click.option("--include-funds", is_flag=True)
@click.option("--include-amendments", is_flag=True)
@click.option("--min-sold", type=float, default=0)
@click.option("--state", default=None)
@click.option("--limit", type=int, default=100)
@click.pass_context
def funding_recent(ctx, days, include_funds, include_amendments, min_sold, state, limit):
    _run_funding(
        ctx,
        "recent",
        days=days,
        include_funds=include_funds,
        include_amendments=include_amendments,
        min_sold=min_sold,
        state=state,
        limit=limit,
    )


@funding.command("search")
@click.argument("keyword")
@click.option("--days", type=int, default=365)
@click.option("--include-funds", is_flag=True)
@click.option("--include-amendments", is_flag=True)
@click.option("--min-sold", type=float, default=0)
@click.option("--state", default=None)
@click.option("--limit", type=int, default=100)
@click.pass_context
def funding_search(ctx, keyword, days, include_funds, include_amendments, min_sold, state, limit):
    _run_funding(
        ctx,
        "search",
        q=keyword,
        days=days,
        include_funds=include_funds,
        include_amendments=include_amendments,
        min_sold=min_sold,
        state=state,
        limit=limit,
    )


@funding.command("company")
@click.option("--cik", default=None)
@click.option("--domain", default=None)
@click.option("--name", default=None)
@click.option("--new-only", is_flag=True, help="Exclude D/A amendments (default includes them).")
@click.option("--include-funds", is_flag=True)
@click.pass_context
def funding_company(ctx, cik, domain, name, new_only, include_funds):
    if not cik and not domain and not name:
        raise click.UsageError("need --cik, --domain, or --name")
    _run_funding(
        ctx,
        "company",
        cik=cik,
        domain=domain,
        q=name,
        include_amendments=not new_only,
        include_funds=include_funds,
    )


@main.command(name="signals")
@click.option("--domain", required=True)
@click.option("--since", default=None)
@click.option("--type", "typ", default=None)
@click.pass_context
def signals_cmd(ctx, domain, since, typ):
    from src.signals.store import SignalStore
    from src.signals.taxonomy import Taxonomy

    db = Database(ctx.obj["config"].storage.db_path)
    store = SignalStore(db, Taxonomy.load())
    rows = store.for_account(domain, since=since, types=[typ] if typ else None)
    for s in rows:
        click.echo(f"{s.observed_at}\t{s.signal_type}\t{s.evidence or s.title or ''}")


@main.command()
@click.option("--domain", required=True)
@click.option("--max-people", type=int, default=8)
@click.pass_context
def deepen(ctx, domain, max_people):
    from src.sources.linkedin_db.collector import request_linkedin_deepen

    request_linkedin_deepen(ctx.obj["config"], domain)
    click.echo(f"deepen requested for {domain}")


@main.command()
@click.option("--cohort", default=None)
@click.option("--skip-collect", is_flag=True)
@click.option("--strict", is_flag=True)
@click.pass_context
def run(ctx, cohort, skip_collect, strict):
    try:
        out = ctx.obj["get_orch"]().run_all(
            cohort=cohort or ctx.obj["cohort"],
            skip_collect=skip_collect,
            skip_seed=True,
            strict=strict,
            dry_run=ctx.obj["dry_run"],
        )
        click.echo("run complete")
        if out.get("errors"):
            click.echo(f"errors={out['errors']}")
    except Exception:
        if strict:
            ctx.exit(1)
        raise


@main.command()
@click.option("--interval-minutes", type=int, default=60)
@click.option("--once", is_flag=True)
@click.pass_context
def watch(ctx, interval_minutes, once):
    from src.pipeline.watch import watch_loop

    n = watch_loop(ctx.obj["get_orch"](), interval_minutes=interval_minutes, once=once, max_iterations=1 if once else None)
    click.echo(f"iterations={n}")


@main.command()
@click.pass_context
def status(ctx):
    from src.pipeline.health import render_status, status_report

    click.echo(render_status(status_report(Database(ctx.obj["config"].storage.db_path), taxonomy=None)))


@main.command(name="plays")
@click.option("--outcome", "outcome", type=click.Choice(["hit", "miss"]), required=True)
@click.option("--domain", required=True)
@click.option("--play", "play_id", required=True)
@click.pass_context
def plays(ctx, outcome, domain, play_id):
    """Record a local play outcome (hit|miss) for backtesting."""
    from src.signals.backtest import record_outcome

    db = Database(ctx.obj["config"].storage.db_path)
    known = db.one(
        "SELECT 1 AS x FROM play_assignments WHERE domain=? AND play_id=? LIMIT 1",
        (domain, play_id),
    )
    if not known:
        click.echo(
            f"warn: no play_assignments row for domain={domain} play={play_id} "
            "(typo? run plays-report to list known plays) — recording anyway"
        )
    record_outcome(db, domain, play_id, outcome)
    click.echo(f"recorded outcome={outcome} domain={domain} play={play_id}")


@main.command(name="plays-report")
@click.pass_context
def plays_report(ctx):
    """Print per-play hit rates (read-only)."""
    from src.signals.backtest import play_hit_rates

    db = Database(ctx.obj["config"].storage.db_path)
    rates = play_hit_rates(db)
    click.echo("play_id\tsent\thit\trate")
    for play_id in sorted(rates):
        r = rates[play_id]
        click.echo(f"{play_id}\t{r['sent']}\t{r['hit']}\t{r['rate']:.3f}")


@main.command(name="plays-calibrate")
@click.option("--min-samples", type=int, default=None, help="Override the 30-sample feed threshold.")
@click.pass_context
def plays_calibrate(ctx, min_samples):
    """Feed decided play outcomes into the calibration table (per source+signal_type).

    Rows below the sample threshold are skipped (calibration blending ignores
    them anyway). Re-running refreshes counts on the same PK.
    """
    from src.signals.backtest import feed_calibration

    db = Database(ctx.obj["config"].storage.db_path)
    kw = {"min_samples": min_samples} if min_samples is not None else {}
    feed_calibration(db, **kw)
    rows = db.query("SELECT source, signal_type, samples, hits FROM calibration ORDER BY source")
    click.echo("source\tsignal_type\tsamples\thits")
    for r in rows:
        click.echo(f"{r['source']}\t{r['signal_type']}\t{r['samples']}\t{r['hits']}")


@main.command()
@click.option("--no-network", is_flag=True)
@click.pass_context
def doctor(ctx, no_network):
    from src.pipeline.health import doctor as run_doctor
    from src.signals.plays import validate_combo_coverage

    rows = run_doctor(ctx.obj["config"], Database(ctx.obj["config"].storage.db_path), check_network=not no_network)
    for name, st, detail in rows:
        click.echo(f"{st}\t{name}\t{detail}")
    gaps = validate_combo_coverage()
    if gaps:
        click.echo(f"WARN\tcombo_coverage\tcombo coverage gaps: {', '.join(gaps)}")


if __name__ == "__main__":
    main()
