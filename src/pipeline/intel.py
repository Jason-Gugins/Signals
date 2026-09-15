"""Coordinator for the master intelligence flow (Task 12).

One domain, every capability, one dossier:

    identity -> collect -> derive -> score -> package

Config and orchestrator are INJECTED. When the caller supplies them the flow
never builds its own -- deliberately NOT the shape of ``src/pipeline/sweep.py``
where ``run_sweep`` constructs a fresh ``Config.load()`` + ``Orchestrator`` and
therefore ignores the caller's config. ``config`` is also never re-read here:
every YAML/dir accessed downstream is the injected object's.

Failure posture: each stage runs in its own try/except; an exception is logged
with ``logger.exception``, recorded under ``errors[<stage>]`` and as a gap, and
the flow continues to the stages whose inputs exist. No stage error ever
escapes ``run_intel``. The ONLY ValueErrors that propagate are the bare-name
target refusal (``parse_target`` -> ``SweepError``) and the dry-run
account-existence check.

Dry run (``dry_run=True``) is genuinely non-fetching: it calls NEITHER
``resolve`` NOR ``find_careers`` NOR the derive pass (the second restricted
collect) NOR ``score`` NOR the package writer. The single permitted call is the
collection planning call with ``dry_run=True``.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from loguru import logger

from src.core.models import Account
from src.export.intel_package import build_dossier, write_intel_package
from src.intel.coverage import build_coverage
from src.intel.market import load_market_profile
from src.pipeline.sweep import parse_target

__all__ = [
    "STAGES",
    "MARKETPLACE_SOURCE_KEYS",
    "LOCAL_DERIVED_SOURCES",
    "run_intel",
]

#: The five workflow stages, in execution order.
STAGES = ("identity", "collect", "derive", "score", "package")

#: The five opt-in marketplace sources (config/sources.yaml ships them all
#: ``enabled: false``); the enabling flag is ``--with-marketplaces``.
MARKETPLACE_SOURCE_KEYS = (
    "marketplace_g2",
    "marketplace_capterra",
    "marketplace_trustradius",
    "marketplace_softwareadvice",
    "marketplace_getapp",
)

#: Local-tier derived sources run as a second, restricted collection pass
#: (no HTTP) so they see the market-profile local context.
LOCAL_DERIVED_SOURCES = ("jobsignals", "needs")

#: LinkedIn-related opt-in source keys that the resolve flag covers.
LINKEDIN_OPT_IN_KEYS = ("linkedin", "linkedin_db")

#: Fallback market profile id when the caller and config name none.
DEFAULT_MARKET_PROFILE_ID = "default"

#: The repo's seller-market relevance profile file.
MARKETS_PATH = Path("config/markets.yaml")

#: Coverage statuses that mean "configured source did not run this pass".
_UNRUN_STATUSES = ("opt_in", "disabled_by_config", "backed_off", "missing_requires")

_MARKETPLACES_FLAG = "--with-marketplaces"
_LINKEDIN_FLAG = "--with-linkedin-resolve"


def _load_config():
    from src.core.config import Config

    return Config.load()


def _build_orchestrator(cfg):
    from src.pipeline.orchestrator import Orchestrator

    return Orchestrator(cfg)


def _profile_id(explicit, cfg) -> str:
    """Explicit arg, else a top-level config override, else the literal default."""
    if explicit:
        return explicit
    override = getattr(cfg, "market_profile_id", None)
    if override:
        return str(override)
    return DEFAULT_MARKET_PROFILE_ID


def _load_profile(profile_id, gaps):
    """Load the market profile; fail soft to ``None`` and record a gap.

    An unknown profile id (``ConfigError``) or an empty profile is NOT fatal:
    the run continues with ``market_profile=None`` so the ``needs`` source
    emits nothing, and a gap explains that no relevance vocabulary is
    configured so nothing can be promoted.
    """
    try:
        profile = load_market_profile(profile_id, path=MARKETS_PATH)
    except Exception as exc:
        gaps.append(
            f"no relevance vocabulary configured: market profile {profile_id!r} "
            f"could not be loaded ({exc}); nothing can be promoted"
        )
        return None
    if getattr(profile, "is_empty", False):
        gaps.append(
            f"no relevance vocabulary configured: market profile {profile_id!r} "
            "is empty; nothing can be promoted"
        )
        return None
    return profile


def _outcomes(stats) -> list:
    """RunnerStats.outcomes (or a dict-shaped stub's) as a list."""
    if isinstance(stats, dict):
        return list(stats.get("outcomes") or ())
    return list(getattr(stats, "outcomes", None) or ())


def _stat(stats, name) -> int:
    if isinstance(stats, dict):
        return int(stats.get(name, 0) or 0)
    return int(getattr(stats, name, 0) or 0)


def _opt_in_flags() -> dict[str, str]:
    """Map configured source keys to the CLI flag that would enable them."""
    flags = {key: _MARKETPLACES_FLAG for key in MARKETPLACE_SOURCE_KEYS}
    for key in LINKEDIN_OPT_IN_KEYS:
        flags[key] = _LINKEDIN_FLAG
    return flags


def _coverage_gap(rows) -> str | None:
    """One summarised gap for configured sources this pass could not run.

    Summarised by status (never enumerated one-per-source) and strictly about
    THIS RUN's coverage -- it never claims a source or fact is absent from the
    world.
    """
    counts: dict[str, int] = {}
    for row in rows or ():
        status = row.get("status")
        if status in _UNRUN_STATUSES:
            counts[status] = counts.get(status, 0) + 1
    if not counts:
        return None
    parts = ", ".join(f"{counts[s]} {s}" for s in _UNRUN_STATUSES if s in counts)
    return (
        f"sources that did not run this pass: {parts}; they are outside this "
        "run's coverage (opt in with the relevant flag or supply the account "
        "field and re-run)"
    )


def _jobs_summary(db, domain: str) -> dict | None:
    """Open-job counts for a domain from the jobs table.

    Open = ``closed_at`` is null. Never raises: a database problem degrades to
    ``None`` (the dossier renders ``jobs: not supplied``) rather than failing
    the package stage.
    """
    if db is None:
        return None
    try:
        rows = db.query(
            "SELECT department, country, closed_at FROM jobs WHERE domain = ?",
            (domain,),
        ) or []
    except Exception:
        logger.exception("jobs summary query failed for {}", domain)
        return None

    by_department: dict[str, int] = {}
    by_country: dict[str, int] = {}
    open_count = 0
    for row in rows:
        closed = row.get("closed_at") if hasattr(row, "get") else None
        if closed:
            continue
        open_count += 1
        dept = (row.get("department") if hasattr(row, "get") else None) or "unknown"
        country = (row.get("country") if hasattr(row, "get") else None) or "unknown"
        by_department[dept] = by_department.get(dept, 0) + 1
        by_country[country] = by_country.get(country, 0) + 1
    return {
        "open_count": open_count,
        "by_department": dict(sorted(by_department.items())),
        "by_country": dict(sorted(by_country.items())),
    }


def _account_count(orc) -> int | None:
    """Rows in the ``accounts`` table, or ``None`` when it cannot be read.

    Used to report how many accounts the global fanout seeded during a run
    (the measured Finding 6 defect: +60 accounts for one company). Never
    raises: a db problem degrades to ``None``.
    """
    db = getattr(orc, "db", None)
    if db is None:
        return None
    try:
        row = db.one("SELECT COUNT(*) AS n FROM accounts")
    except Exception:
        row = None
    if row is not None:
        try:
            return int(row["n"] if hasattr(row, "__getitem__") else row)
        except Exception:
            return None
    try:
        return len(db.query("SELECT domain FROM accounts"))
    except Exception:
        return None


def run_intel(
    target: str,
    *,
    name: str | None = None,
    market_profile_id: str | None = None,
    skip: tuple[str, ...] = (),
    force: bool = False,
    dry_run: bool = False,
    with_marketplaces: bool = False,
    with_linkedin_resolve: bool = False,
    include_fanout: bool = False,
    write: bool = True,
    max_signals: int | None = None,
    config=None,
    orch=None,
) -> dict:
    """Run the full intelligence flow for one domain.

    Returns ``{domain, created, stages, errors, coverage, gaps, dossier,
    paths}``. See the module docstring for the failure and dry-run contracts.
    """
    domain = parse_target(target)
    skip = tuple(skip or ())

    # A per-invocation correlation id, minted once here and carried into the
    # dossier (and therefore into the package directory and manifest) by
    # ``build_dossier``. It correlates ARTIFACTS from this run only; it is not
    # a parent/child run-log id (see the module docstring).
    invocation_id = uuid.uuid4().hex[:8]

    cfg = config if config is not None else _load_config()
    orc = orch if orch is not None else _build_orchestrator(cfg)

    registry = orc.registry
    account = registry.get(domain)
    created = account is None

    if dry_run and account is None:
        raise ValueError(
            f"dry-run requires an existing account: {domain} is not in the "
            "registry (run without --dry-run to create it)"
        )

    if not dry_run:
        if account is None:
            account = Account(
                domain=domain,
                name=name or domain.split(".")[0].capitalize(),
            )
            registry.upsert(account, source="intel")
        elif name and account.name != name:
            account.name = name
            registry.upsert(account, source="intel")
        account = registry.get(domain) or account

    stages: dict[str, dict] = {}
    errors: dict[str, str] = {}
    gaps: list[str] = []
    outcomes: list = []
    coverage_rows: list[dict] = []
    dossier = None
    paths: dict = {}
    snapshot = None
    market_profile = None
    profile_id = _profile_id(market_profile_id, cfg)

    def record(stage: str, status: str, **info) -> None:
        info["status"] = status
        stages[stage] = info

    # -- identity -----------------------------------------------------------
    if dry_run:
        record("identity", "skipped", note="dry-run: resolution is not performed")
    elif "identity" in skip:
        record("identity", "skipped", note="skipped by request")
    else:
        try:
            resolved = orc.resolve(
                domains=[domain],
                ats=True,
                cik=True,
                feeds=True,
                icp=True,
                appstore=True,
                bbb=True,
                g2=with_marketplaces,
                linkedin=with_linkedin_resolve,
            )
            # Re-read so fields filled by the resolvers (careers_url,
            # ats_vendor, cik, icp_fit) are visible to later stages.
            account = registry.get(domain) or account
            record("identity", "ran", resolved=resolved if isinstance(resolved, dict) else {})
        except Exception as exc:
            logger.exception("intel identity stage failed for {}", domain)
            errors["identity"] = str(exc)
            record("identity", "failed", reason=str(exc))
            gaps.append(f"identity stage failed: {exc}")
            account = registry.get(domain) or account

    # -- collect ------------------------------------------------------------
    include_disabled = set(MARKETPLACE_SOURCE_KEYS) if with_marketplaces else None
    # Finding 6: the three global fanout adapters are opt-in. Count the
    # accounts table around the PRIMARY pass so an included fanout can report
    # how many accounts it seeded.
    accounts_before = (
        _account_count(orc) if (include_fanout and not dry_run) else None
    )
    if "collect" in skip:
        record("collect", "skipped", note="skipped by request")
    else:
        try:
            stats = orc.collect(
                sources=None,
                domains=[domain],
                force=force,
                dry_run=dry_run,
                limit=None,
                include_disabled_sources=include_disabled,
                local_context=None,
                skip_fanout=not include_fanout,
            )
            outcomes.extend(_outcomes(stats))
            if dry_run:
                record(
                    "collect",
                    "ran",
                    dry_run=True,
                    planned_tasks=_stat(stats, "tasks"),
                    sources_planned=len(_outcomes(stats)),
                )
            else:
                record(
                    "collect",
                    "ran",
                    tasks=_stat(stats, "tasks"),
                    signals_new=_stat(stats, "signals_new"),
                )
        except Exception as exc:
            logger.exception("intel collect stage failed for {}", domain)
            errors["collect"] = str(exc)
            record("collect", "failed", reason=str(exc))
            gaps.append(f"collect stage failed: {exc}")

    # -- fanout honesty (Finding 6) ------------------------------------------
    # The dossier must say which posture this run took: the globals were
    # skipped (opt in next time) or ran and seeded N unrelated accounts.
    if include_fanout:
        accounts_after = _account_count(orc)
        if accounts_before is None or accounts_after is None:
            delta = None
        else:
            delta = accounts_after - accounts_before
        gaps.append(
            "fanout sources ran (sec_formd, federal_register, warn_notices) "
            f"- accounts created during collect: {delta if delta is not None else 'unknown'}"
        )
    else:
        gaps.append(
            "fanout sources skipped (sec_formd, federal_register, warn_notices) "
            "- pass --include-fanout"
        )

    # -- derive -------------------------------------------------------------
    # The market profile is loaded FIRST: it is the relevance vocabulary the
    # local harvesters need, and the loading decision (None on empty/unknown)
    # must be settled before the pass runs.
    if dry_run:
        record("derive", "skipped", note="dry-run: the local derived pass is not executed")
    elif "derive" in skip:
        record("derive", "skipped", note="skipped by request")
    else:
        try:
            market_profile = _load_profile(profile_id, gaps)
            derived = orc.collect(
                sources=list(LOCAL_DERIVED_SOURCES),
                domains=[domain],
                force=True,
                dry_run=False,
                limit=None,
                include_disabled_sources=None,
                local_context={
                    "market_profile": market_profile,
                    "market_profile_id": profile_id,
                },
            )
            outcomes.extend(_outcomes(derived))
            record(
                "derive",
                "ran",
                sources=list(LOCAL_DERIVED_SOURCES),
                signals_new=_stat(derived, "signals_new"),
            )
        except Exception as exc:
            logger.exception("intel derive stage failed for {}", domain)
            errors["derive"] = str(exc)
            record("derive", "failed", reason=str(exc))
            gaps.append(f"derive stage failed: {exc}")

    # -- score --------------------------------------------------------------
    if dry_run:
        record("score", "skipped", note="dry-run: scoring is not performed")
    elif "score" in skip:
        record("score", "skipped", note="skipped by request")
    else:
        try:
            scored = orc.score(domains=[domain], return_snapshots=True)
            snapshots = scored.get("snapshots") if isinstance(scored, dict) else None
            snapshot = (snapshots or {}).get(domain)
            if snapshot is None:
                # A missing snapshot mapping is a stage FAILURE, never an
                # empty dossier.
                msg = f"score returned no snapshot for {domain}"
                errors["score"] = msg
                record("score", "failed", reason=msg)
                gaps.append(f"score stage failed: {msg}")
            else:
                record(
                    "score",
                    "ran",
                    scored=(scored.get("scored") if isinstance(scored, dict) else None),
                )
        except Exception as exc:
            logger.exception("intel score stage failed for {}", domain)
            errors["score"] = str(exc)
            record("score", "failed", reason=str(exc))
            gaps.append(f"score stage failed: {exc}")

    # -- package ------------------------------------------------------------
    if dry_run:
        record("package", "skipped", note="dry-run: no package is built or written")
    elif "package" in skip:
        record("package", "skipped", note="skipped by request")
    elif snapshot is None:
        record(
            "package",
            "skipped",
            note="no snapshot available for this domain; package not built",
        )
    else:
        try:
            coverage_rows = build_coverage(
                config=cfg,
                account=account,
                outcomes=outcomes,
                opt_in_flags=_opt_in_flags(),
                requested=None,
            )
            coverage_gap = _coverage_gap(coverage_rows)
            if coverage_gap:
                gaps.append(coverage_gap)
            jobs_summary = _jobs_summary(getattr(orc, "db", None), domain)
            dossier = build_dossier(
                snapshot,
                coverage=coverage_rows,
                market_profile=market_profile,
                market_profile_id=profile_id,
                jobs_summary=jobs_summary,
                gaps=gaps,
                max_signals=max_signals,
                invocation_id=invocation_id,
            )
            if write:
                paths = write_intel_package(
                    dossier, out_dir=cfg.storage.dossiers_dir
                )
            record(
                "package",
                "ran",
                written=bool(write),
                coverage_rows=len(coverage_rows),
            )
        except Exception as exc:
            logger.exception("intel package stage failed for {}", domain)
            errors["package"] = str(exc)
            record("package", "failed", reason=str(exc))
            gaps.append(f"package stage failed: {exc}")

    return {
        "domain": domain,
        "created": created,
        "stages": stages,
        "errors": errors,
        "coverage": coverage_rows,
        "gaps": gaps,
        "dossier": dossier,
        "paths": paths or {},
    }