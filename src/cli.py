"""Signals CLI."""

from __future__ import annotations

import sys
from pathlib import Path

import click
from loguru import logger

from src.core.config import Config
from src.core.db import Database
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
@click.option("--limit", type=int, default=None)
@click.pass_context
def resolve(ctx, cik, ats, feeds, icp, limit):
    orch: Orchestrator = ctx.obj["get_orch"]()
    try:
        out = orch.resolve(cohort=ctx.obj["cohort"], limit=limit, ats=ats, cik=cik, feeds=feeds, icp=icp)
        click.echo(f"resolved accounts={out.get('accounts', 0)}")
    except Exception as exc:
        click.echo(f"resolve degraded: {exc}")


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
    paths = ctx.obj["get_orch"]().export(cohort=ctx.obj["cohort"], fmt=fmt)
    for p in paths:
        click.echo(p)


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


@main.command()
@click.option("--no-network", is_flag=True)
@click.pass_context
def doctor(ctx, no_network):
    from src.pipeline.health import doctor as run_doctor

    rows = run_doctor(ctx.obj["config"], Database(ctx.obj["config"].storage.db_path), check_network=not no_network)
    for name, st, detail in rows:
        click.echo(f"{st}\t{name}\t{detail}")


if __name__ == "__main__":
    main()
