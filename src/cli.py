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
@click.option("--appstore/--no-appstore", default=False)
@click.option("--bbb/--no-bbb", default=False)
@click.option("--linkedin/--no-linkedin", default=False)
@click.option("--limit", type=int, default=None)
@click.pass_context
def resolve(ctx, cik, ats, feeds, icp, g2, capterra, appstore, bbb, linkedin, limit):
    orch: Orchestrator = ctx.obj["get_orch"]()
    if capterra:
        _resolve_capterra(orch, cohort=ctx.obj["cohort"], limit=limit)
        return
    try:
        out = orch.resolve(cohort=ctx.obj["cohort"], limit=limit, ats=ats, cik=cik, feeds=feeds, icp=icp, g2=g2, appstore=appstore, bbb=bbb, linkedin=linkedin)
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


def write_digest(path: str, text: str) -> str:
    """Write a digest doc, creating the parent directory if needed."""
    from pathlib import Path as _Path

    p = _Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return str(p)


def _email_files(paths, cfg: Config, kind: str, period: str, to: str) -> None:
    """Email written brief/digest files. Send failures log and continue —
    the files are already written either way."""
    import os

    from src.export.email import send_email

    smtp = getattr(cfg, "smtp", None)
    if not smtp or not smtp.host:
        logger.warning("--email requested but SMTP not configured (set SIGNALS_SMTP_HOST); skipping email")
        return
    if not to:
        to = os.environ.get("SIGNALS_EMAIL_TO", "")
    if not to:
        logger.warning("--email requested but no recipient (use --to or SIGNALS_EMAIL_TO); skipping email")
        return
    for p in paths:
        path = Path(p)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("email skipped: cannot read {}: {}", p, exc)
            continue
        name = path.stem
        subject = f"Signals {period} {kind} — {name}" if period else f"Signals {kind} — {name}"
        ok = send_email(
            subject,
            text,
            to=to,
            host=smtp.host,
            port=smtp.port,
            user_env=smtp.user_env,
            pass_env=smtp.pass_env,
            use_tls=smtp.use_tls,
        )
        if ok:
            click.echo(f"emailed {kind}: {p} -> {to}")


def _digest_paths(ctx, period, domains, include_tier4=False, include_formd=True) -> list[str]:
    """Build and write per-account digests; returns the written paths.

    ``include_formd`` enables the "New Form D issuers (unmatched)" section.
    It only ever renders on stub accounts (cik*.edgar), so passing True
    unconditionally is safe for per-domain renders too — a named account's
    digest simply never contains stub signals.
    """
    from src.export.digest import build_digest
    from src.pipeline.orchestrator import _today
    from src.signals.calibration import load_stats
    from src.signals.combos import evaluate_combos
    from src.signals.plays import assign_plays
    from src.signals.score import score_account
    from src.signals.tier import assign_tier

    orch = ctx.obj["get_orch"]()
    cfg = ctx.obj["config"]
    scoring = cfg.load_yaml("scoring")
    plays_cfg = cfg.load_yaml("plays")
    # Supersede-aware (parity with the score path in orchestrator.score):
    # expired signals must not inflate tier/score/signal groups in digests.
    from src.signals.lifecycle import load_supersede_map, partition_signals

    try:
        supersede_map = load_supersede_map(cfg.load_yaml("signals"))
    except FileNotFoundError:
        supersede_map = {}
    calibration_stats = load_stats(orch.db)
    today = _today()
    # Window the digest: daily = last 1 day, weekly = last 7 (inclusive of
    # today). Signals observed before the cutoff are excluded by build_digest.
    from datetime import timedelta

    days = {"daily": 1, "weekly": 7}.get(period)
    since = (today - timedelta(days=days)).isoformat() if days else None
    digests_dir = getattr(cfg.storage, "digests_dir", "data/digests")
    paths = []
    for acct in orch._accounts(domains=list(domains) or None, cohort=ctx.obj["cohort"]):
        signals = orch.signal_store.for_account(acct.domain)
        signals, _expired = partition_signals(
            signals, today=today, supersede_days_by_type=supersede_map
        )
        combos = evaluate_combos(signals, scoring.get("combos") or [], today=today)
        result = score_account(acct, signals, taxonomy=orch.taxonomy, cfg=scoring, today=today, combos=combos, calibration_stats=calibration_stats)
        tier = assign_tier(signals, result, taxonomy=orch.taxonomy, cfg=scoring, today=today)
        contacts = orch._contacts(acct.domain)
        if tier.tier >= 4 and include_tier4:
            # Tier 4 (dormant) = nurture: include in the digest, no plays.
            plays = []
        else:
            plays = assign_plays(acct, signals, result, tier, taxonomy=orch.taxonomy, plays_cfg=plays_cfg, contacts=contacts)
        text = build_digest(acct.domain, signals, plays, period=period, taxonomy=orch.taxonomy, since=since, include_formd_unmatched=include_formd)
        paths.append(write_digest(str(Path(digests_dir) / f"{acct.domain}.md"), text))
    return paths


@main.command()
@click.option("--period", type=click.Choice(["daily", "weekly"]), default="daily")
@click.option("--domain", "domains", multiple=True)
@click.option("--email", "email_flag", is_flag=True, help="Email the digest(s) via SMTP (SIGNALS_SMTP_HOST).")
@click.option("--to", "to_addr", default=None, help="Email recipient (default: SIGNALS_EMAIL_TO).")
@click.option("--tier-4", "include_tier4", is_flag=True, help="Include tier-4 (dormant) accounts as nurture digests with no plays.")
@click.pass_context
def digest(ctx, period, domains, email_flag, to_addr, include_tier4):
    """Per-account alert digest (markdown) written to data/digests/."""
    cfg = ctx.obj["config"]
    if include_tier4:
        ctx.obj["include_tier4"] = True
    # Flag reaches _digest_paths via ctx.obj (callers/tests that monkeypatch
    # _digest_paths(ctx, period, domains) keep working; the kwarg is read
    # from ctx.obj here so the flag is never dropped on the floor).
    paths = _digest_paths(ctx, period, domains, include_tier4=ctx.obj.get("include_tier4", False))
    for p in paths:
        click.echo(p)
    if email_flag:
        _email_files(paths, cfg, kind="digest", period=period, to=to_addr or "")


@main.command()
@click.option("--domain", "domains", multiple=True)
@click.option("--tier-max", type=int, default=2)
@click.option("--open", "open_files", is_flag=True)
@click.option("--email", "email_flag", is_flag=True, help="Email the brief(s) via SMTP (SIGNALS_SMTP_HOST).")
@click.option("--to", "to_addr", default=None, help="Email recipient (default: SIGNALS_EMAIL_TO).")
@click.pass_context
def brief(ctx, domains, tier_max, open_files, email_flag, to_addr):
    paths = _brief_paths(ctx, domains, tier_max)
    for p in paths:
        click.echo(p)
    if open_files:
        click.echo("(open skipped)")
    if email_flag:
        _email_files(paths, ctx.obj["config"], kind="brief", period="", to=to_addr or "")


def _brief_paths(ctx, domains, tier_max) -> list[str]:
    return ctx.obj["get_orch"]().brief(
        domains=list(domains) or None, cohort=ctx.obj["cohort"], tier_max=tier_max
    )


def _deliver_alert_destinations(ctx) -> list[str]:
    """Plan Task 14 wiring: deliver new alerts through every configured destination.

    Reads ``exports.destinations`` via load_destinations (default resolves to a
    single file destination appending to alerts.jsonl — the pre-plugin file
    convention — so the default adds no double-write). Webhook destinations
    delegate to the existing signed deliver_alerts path. Alerts whose natural
    key is already present in alerts.jsonl are skipped so repeated same-day
    exports do not duplicate file rows.

    Returns per-destination outcome strings.
    """
    from src.export.alerts import _natural_key, build_alerts
    from src.export.destinations import load_destinations
    from src.pipeline.orchestrator import _today

    cfg = ctx.obj["config"]
    alerts = build_alerts(ctx.obj["get_orch"]().db, since=_today().isoformat(), min_tier=1)
    if not alerts:
        return []
    # File-side dedupe: drop alerts already recorded in alerts.jsonl.
    try:
        alerts_path = Path(getattr(cfg.storage, "alerts_dir", "data/alerts")) / "alerts.jsonl"
        seen: set[tuple] = set()
        if alerts_path.exists():
            import json as _json

            for line in alerts_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = _json.loads(line)
                    seen.add((row.get("domain"), row.get("signal_type"), row.get("at")))
                except ValueError:
                    continue
        alerts = [a for a in alerts if _natural_key(a) not in seen]
    except Exception:
        logger.exception("alerts.jsonl dedupe read failed; delivering all")
    if not alerts:
        return []
    results = []
    for dest in load_destinations(cfg):
        try:
            results.append(dest.deliver(alerts, cfg))
        except Exception:
            logger.exception("destination {} failed", type(dest).__name__)
    return results


@main.command(name="export")
@click.option("--format", "fmt", type=click.Choice(["csv", "json"]), default="csv")
@click.option("--what", default="all")
@click.pass_context
def export_cmd(ctx, fmt, what):
    paths = ctx.obj["get_orch"]().export(cohort=ctx.obj["cohort"], fmt=fmt, what=what)
    for p in paths:
        click.echo(p)
    # Config-driven destination delivery (Plan Task 14): no-op unless alerts
    # exist; default config resolves to the plain alerts.jsonl file sink.
    for r in _deliver_alert_destinations(ctx):
        click.echo(r)


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


def _selfcheck_source(source: str, *, domain: str | None = None,
                      token: str | None = None, url: str | None = None):
    """Map a generic selfcheck source -> (url, fetch_source, fetch_domain,
    extract, markup_hints), building the sample URL from the account fields.

    techstack      — homepage of ``domain``, fingerprint match over the HTML
    news_rss       — the account's blog/feed URL (or explicit ``--url``)
    ats_greenhouse — Greenhouse board JSON for the account's ATS token

    Returns ``(url, fetch_source, fetch_domain, extract, markup_hints,
    detect_challenge)``.
    """
    if source == "techstack":
        from src.sources.techstack.fingerprint import classify_cloudflare_challenge

        d = domain or (url and url.split("/")[2]) or "example.com"
        sample = url or f"https://{d}/"

        def extract(html: str, _u: str = sample):
            from src.sources.techstack.fingerprint import (
                extract_http_evidence,
                load_fingerprint_rules,
                match_fingerprints,
            )
            ev = extract_http_evidence(html.encode("utf-8", "replace"), {}, _u)
            return match_fingerprints(ev, load_fingerprint_rules())

        detect = lambda status, body: (  # noqa: E731
            classify_cloudflare_challenge(status=status, body=body) is not None)
        return sample, "techstack", d, extract, [b"technologies", b"__NEXT_DATA__"], detect

    if source == "news_rss":
        if not url:
            raise click.UsageError("--url (or the account's blog_feed_url) is required for news_rss")
        from src.sources.news.feeds import parse_feed

        detect = lambda status, body: (  # noqa: E731
            b"Just a moment" in body or b"cf-chl" in body)
        return (url, "news_rss", url.split("/")[2],
                lambda html: parse_feed(html.encode("utf-8", "replace")),
                [b"<item", b"<entry"], detect)

    if source == "ats_greenhouse":
        if not token:
            raise click.UsageError("--token (the account's ats_token) is required for ats_greenhouse")
        from src.sources.ats.greenhouse import parse_greenhouse

        sample = (f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
                  f"?content=true")
        detect = lambda status, body: (  # noqa: E731
            b'"maintenance"' in body or b"Just a moment" in body)
        return (sample, "ats_greenhouse", "boards-api.greenhouse.io",
                lambda html: parse_greenhouse(html.encode("utf-8", "replace")),
                [b"jobs"], detect)

    raise click.UsageError(f"unknown selfcheck source: {source}")


@main.command(name="selfcheck")
@click.option("--source", "source", required=True,
              type=click.Choice(["techstack", "news_rss", "ats_greenhouse"]),
              help="Which source parser to self-check.")
@click.option("--domain", default=None, help="Account domain (techstack sample URL).")
@click.option("--token", default=None, help="Account ATS token (ats_greenhouse).")
@click.option("--url", default=None, help="Explicit sample URL (news_rss feed URL, etc.).")
@click.pass_context
def selfcheck(ctx, source, domain, token, url):
    """Generic selector-drift self-check for non-marketplace sources.

    Fetches one sample document and verifies the source parser still extracts
    items: ok / drift (markup present, 0 parsed) / empty / challenge / error.
    Uses the stealth-browser fetcher (same as g2-selfcheck).
    """
    from src.core.patchright_browser import PatchrightBrowserFetcher
    from src.core.selfcheck import run_source_selfcheck

    cfg = ctx.obj["config"]
    sample_url, fetch_source, fetch_domain, extract, hints, detect = (
        _selfcheck_source(source, domain=domain, token=token, url=url))
    fetcher = PatchrightBrowserFetcher(cfg, _RawShim())
    r = run_source_selfcheck(fetcher, url=sample_url, source=fetch_source,
                             domain=fetch_domain, extract=extract,
                             markup_hints=hints, detect_challenge=detect)
    click.echo(f"state={r.state} items={r.review_count} url={r.url} {r.detail}")
    if r.state in ("drift", "error"):
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
@click.option("--timeout", type=int, default=900)
@click.pass_context
def deepen(ctx, domain, max_people, timeout):
    """Trigger the companion LinkedIn scraper for one company (manual, gated)."""
    from src.sources.linkedin_db.collector import request_linkedin_deepen

    request_linkedin_deepen(ctx.obj["config"], domain, max_people=max_people, timeout=timeout)
    click.echo(f"deepen requested for {domain}")


def _discover_label(cand: dict) -> str:
    """Best display label across stage candidate shapes (wikidata label,
    wikipedia/ddg title, gkg name)."""
    return str(cand.get("label") or cand.get("title") or cand.get("name") or "")


def _run_discover(ctx, names: list[str], *, ddg: bool = False) -> None:
    """Opt-in name->domain discovery report (plan T4).

    BYPASSES parse_target and the sweep pipeline entirely: nothing here
    creates accounts — ranked candidates are queued in identity_candidates
    and the human promotes one by rerunning `sweep <chosen-domain>`.
    """
    orch: Orchestrator = ctx.obj["get_orch"]()
    for name in names:
        try:
            result = orch.discover(name, ddg=ddg)
        except Exception as exc:
            click.echo(f"{name}: discovery failed: {exc}", err=True)
            continue
        status = result.get("status")
        domain = result.get("domain")
        if status == "resolved" and domain:
            agreement = result.get("agreement")
            if agreement:
                suffix = f" (agreement: {' + '.join(agreement)})"
            else:
                stages = result.get("stages") or {}
                hit_stage = next(
                    (s for s in stages if (stages.get(s) or {}).get("status") == "resolved"),
                    None,
                )
                suffix = f" (via {hit_stage})" if hit_stage else ""
            click.echo(f"{name}: resolved -> {domain}{suffix}")
        else:
            click.echo(f"{name}: {status} ({len(result.get('candidates') or [])} candidates)")
        for cand in result.get("candidates") or []:
            click.echo(
                f"  {cand.get('score')}\t{cand.get('stage')}\t{cand.get('domain') or ''}"
                f"\t{_discover_label(cand)}\t{cand.get('url') or cand.get('p856_url') or ''}"
            )
        if result.get("queued"):
            top = next(
                (c.get("domain") for c in result.get("candidates") or [] if c.get("domain")),
                None,
            )
            hint = f"sweep {top}" if top else "sweep <chosen-domain>"
            click.echo(f"queued to identity_candidates; promote with: {hint}")


@main.command()
@click.argument("url_or_name", required=False)
@click.option("--force", is_flag=True, help="Force recollection even if the account already exists.")
@click.option("--deep", is_flag=True, help="Run the identity resolver pass (CIK/ATS/feeds/ICP) even for existing accounts.")
@click.option(
    "--discover",
    "discover_names",
    multiple=True,
    help="Company-name discovery instead of a sweep: resolve NAME to a domain via the keyless "
    "waterfall (Wikidata -> Wikipedia -> GKG) and queue ranked candidates in identity_candidates "
    "for human review. Repeatable. Never creates accounts.",
)
@click.option(
    "--ddg",
    is_flag=True,
    default=False,
    help="Also try the DuckDuckGo SERP stage (resolve-time only, may be bot-gated).",
)
@click.pass_context
def sweep(ctx, url_or_name, force, deep, discover_names, ddg):
    """One-command onboarding: seed-or-update the account, then collect.

    Accepts a URL (https://acme.io/about) or a bare domain (acme.io).
    A bare company name without a dot is refused in v1 — use
    `sweep --discover "Company Name"` to queue domain candidates instead.
    """
    from src.pipeline import sweep as sweep_mod

    if ddg and not discover_names:
        raise click.UsageError("--ddg requires --discover NAME")
    if discover_names:
        if url_or_name:
            raise click.UsageError("pass either a target or --discover NAME, not both")
        _run_discover(ctx, list(discover_names), ddg=ddg)
        return
    if not url_or_name:
        raise click.UsageError("Missing argument 'URL_OR_NAME' (or pass --discover NAME).")
    try:
        result = sweep_mod.run_sweep(url_or_name, force_first_run=True if force else None, deep=deep)
    except sweep_mod.SweepError as exc:
        click.echo(f"sweep refused: {exc}", err=True)
        ctx_exit(2)
        return
    created = "created" if result["created"] else "existing"
    click.echo(f"account={result['domain']} ({created})")
    resolved = result.get("resolved") or {}
    if resolved:
        click.echo(
            f"resolved cik={resolved.get('cik', 0)} ats={resolved.get('ats', 0)} "
            f"feeds={resolved.get('feeds', 0)} icp={resolved.get('icp', 0)}"
        )
    collected = result.get("collected") or {}
    click.echo(
        f"fetched={collected.get('fetched', 0)} "
        f"signals_new={collected.get('signals_new', 0)} "
        f"failed={collected.get('failed', 0)}"
    )
    if result.get("skipped"):
        click.echo("sources skipped and why:")
        for source, reason in result["skipped"]:
            click.echo(f"  {source}\t{reason}")
    for line in result.get("reminders", []):
        click.echo(f"reminder: {line}")


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
    from src.pipeline.health_report import source_health

    db = Database(ctx.obj["config"].storage.db_path)
    click.echo(render_status(status_report(
        db,
        taxonomy=None,
        raw_quota_mb=ctx.obj["config"].storage.raw_quota_mb,
    )))
    # Per-source health table (read-only, from fetch_log).
    click.echo("source\tfetched\tfailed\trate\ttop_error_class")
    for r in source_health(db):
        top = max(r["error_class"].items(), key=lambda kv: kv[1])[0] if r["error_class"] else ""
        click.echo(f"{r['source']}\t{r['fetched']}\t{r['failed']}\t{r['success_rate']:.2f}\t{top}")


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
