"""Sweep: one-command account onboarding + full-source collection.

v1 scope (binding decisions):
- Accept a URL (registrable domain extracted) or a bare domain (contains a
  dot). A bare company NAME (no dot) is refused — no fuzzy company search;
  bare-name discovery is available only via the explicit --discover flag,
  which queues candidates for human review and never creates accounts.
- No auto-discovery of CIK/blog_feed_url/linkedin_slug: sources whose
  ``requires`` the account lacks are reported as skipped with a reason.
- Deepen is NOT triggered here (manual-only posture preserved); a reminder
  line is returned instead.
- collect() runs with force=True only when the account is newly created
  (first run); otherwise source cadences are respected.
"""

from __future__ import annotations

from urllib.parse import urlparse

from loguru import logger

from src.core.models import Account
from src.identity.domains import root_domain

__all__ = ["run_sweep", "SweepError"]


class SweepError(ValueError):
    """Refused input (e.g. bare company name with no domain)."""


def orchestrator_collect(orch, **kw):
    """Indirect dispatch target so tests can patch sweep-level, without
    touching Orchestrator for every caller."""
    return orch.collect(**kw)


def _enabled_adapters(orch):
    """Enabled source adapters for the orchestrator's config (patch seam).

    Tolerates a stubbed orchestrator (CLI tests no-op __init__) by falling
    back to the empty list — nothing to report as skipped in that case.
    """
    cfg = getattr(orch, "config", None)
    if cfg is None:
        return []
    from src.sources.registry import enabled_sources

    return enabled_sources(cfg)


def parse_target(url_or_name: str) -> str:
    """URL or bare domain → registrable domain; bare company name refused."""
    raw = (url_or_name or "").strip()
    if not raw:
        raise SweepError("empty input: pass a URL or a domain (e.g. https://acme.io or acme.io)")
    host = urlparse(raw if "://" in raw else f"https://{raw}").hostname
    if not host and "://" not in raw:
        # urlparse put everything in path for inputs like "acme.io/x"
        host = raw.split("/")[0]
    if host and "." in host:
        domain = root_domain(raw)
        if domain:
            return domain
    raise SweepError(
        f"{url_or_name!r} looks like a company name, not a domain. "
        "Sweep v1 needs a URL or bare domain (e.g. https://acme.io or acme.io) — "
        "company-name search is not supported yet."
    )


def _missing_requires(account: Account, adapter) -> list[str]:
    missing = []
    for field in getattr(adapter, "requires", ()):
        if getattr(account, field, None) in (None, ""):
            missing.append(field)
    return missing


def run_sweep(
    url_or_name: str,
    *,
    force_first_run: bool | None = None,
    deep: bool = False,
) -> dict:
    """Seed-or-update the account, collect from every applicable source.

    Newly created accounts also get a resolver pass (Orchestrator.resolve:
    CIK/ATS/feeds/ICP), reported under ``resolved``. With ``deep=True`` the
    resolver pass runs even for existing accounts.

    Returns {domain, created, collected, skipped, reminders[, resolved, deep]}.
    """
    from src.core.config import Config
    from src.core.db import Database
    from src.identity.registry import AccountRegistry
    from src.pipeline.orchestrator import Orchestrator

    domain = parse_target(url_or_name)

    cfg = Config.load()
    db = Database(cfg.storage.db_path)
    registry = AccountRegistry(db)
    orch = Orchestrator(cfg, db=db)

    existing = registry.get(domain)
    created = existing is None
    name = (existing.name if existing else None) or domain.split(".")[0].capitalize()
    if not existing:
        registry.upsert(Account(domain=domain, name=name), source="sweep")

    adapters = _enabled_adapters(orch)
    account = registry.get(domain) or Account(domain=domain, name=name)

    def _compute_skipped() -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for adapter in adapters:
            missing = _missing_requires(account, adapter)
            if missing:
                out.append((adapter.key, "missing " + ", ".join(missing)))
        return out

    skipped = _compute_skipped()

    # Resolver pass: on onboarding (newly created) or when --deep is passed.
    # Deliberately omits appstore/bbb/linkedin: they are explicit opt-in flags
    # on `resolve` (network/subprocess cost; LinkedIn posture is
    # human-triggered), so sweep keeps to the always-on resolvers only.
    # Resolution failures must never fail the sweep.
    resolved: dict = {}
    if created or deep:
        try:
            out = orch.resolve(domains=[domain], ats=True, cik=True, feeds=True, icp=True, g2=False)
            resolved = {k: out.get(k, 0) for k in ("cik", "ats", "feeds", "icp")}
        except Exception as exc:
            logger.warning("sweep resolver pass failed for {}: {}", domain, exc)
            resolved = {}
        else:
            # Re-fetch post-resolve so fields filled by resolvers no longer
            # show as missing in the skipped report.
            account = registry.get(domain) or account
            skipped = _compute_skipped()

    force = created if force_first_run is None else bool(force_first_run)
    stats = orchestrator_collect(orch, sources=None, domains=[domain], force=force, dry_run=False, limit=None, cohort=None)
    if isinstance(stats, dict):
        collected = {k: stats.get(k, 0) for k in ("fetched", "signals_new", "failed")}
    else:
        collected = {
            "fetched": getattr(stats, "fetched", 0),
            "signals_new": getattr(stats, "signals_new", 0),
            "failed": getattr(stats, "failed", 0),
        }

    reminders = [
        "LinkedIn people data is deepen-driven: run `python -m src.cli deepen --domain "
        f"{domain}` when you want profiles scraped (manual-only posture)."
    ]
    result = {
        "domain": domain,
        "created": created,
        "collected": collected,
        "skipped": skipped,
        "reminders": reminders,
        "resolved": resolved,
    }
    if deep:
        result["deep"] = True
    return result
