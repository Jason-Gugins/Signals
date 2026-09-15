"""Status report and doctor diagnostics."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


MB = 1024 * 1024


def raw_quota_check(raw_mb: float, quota_mb: float | None) -> dict | None:
    """Storage-quota check for the raw document store.

    Returns None when disabled (quota_mb is None/0), otherwise
    {"status": "OK"|"WARN", "used_mb", "quota_mb"} — WARN when the raw
    byte total exceeds the configured quota.
    """
    if not quota_mb or quota_mb <= 0:
        return None
    quota_mb = float(quota_mb)
    status = "WARN" if raw_mb > quota_mb else "OK"
    return {"status": status, "used_mb": round(raw_mb, 1), "quota_mb": quota_mb}


#: A run older than this (hours) that is still 'running' cannot be alive.
#: Must exceed the longest expected run; `signals prune --stale-run-hours`
#: overrides it. runs.started_at is always aware-UTC isoformat written by
#: RunContext (src/core/runlog.py:42-43), which is what makes the string
#: comparison below valid; a space-separated naive writer would break it.
DEFAULT_STALE_RUN_HOURS = 6


def stale_running_runs(db, *, now=None, max_age_hours: int = DEFAULT_STALE_RUN_HOURS) -> list[dict]:
    """runs rows still marked running whose start is older than the cutoff.

    A killed process never reaches RunContext.__exit__ (runlog.py:74), so the
    row stays 'running' forever and misleads status/doctor. Read-only: the
    caller decides whether to repair.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=max_age_hours)).replace(microsecond=0).isoformat()
    return db.query(
        "SELECT run_id, stage, started_at FROM runs WHERE status='running' AND started_at < ? "
        "ORDER BY started_at",
        (cutoff,),
    )


def finalize_stale_runs(db, *, now=None, max_age_hours: int = DEFAULT_STALE_RUN_HOURS) -> int:
    """Mark stale running rows failed; returns how many were finalized.

    Safe to re-run: the UPDATE is guarded on status='running', so a live
    process that finishes in the meantime is never touched.
    """
    now = now or datetime.now(timezone.utc)
    stamp = now.replace(microsecond=0).isoformat()
    rows = stale_running_runs(db, now=now, max_age_hours=max_age_hours)
    for row in rows:
        db.execute(
            "UPDATE runs SET status='failed', finished_at=?, notes=? "
            "WHERE run_id=? AND status='running'",
            (stamp, "finalized by prune: process did not complete", row["run_id"]),
        )
    return len(rows)


def status_report(db, *, taxonomy, raw_quota_mb: float | None = None) -> dict:
    accounts = db.query("SELECT tier, disqualified FROM accounts")
    by_tier = {}
    dq = 0
    for a in accounts:
        if a.get("disqualified"):
            dq += 1
        t = a.get("tier")
        by_tier[str(t)] = by_tier.get(str(t), 0) + 1
    sigs = db.query("SELECT category, observed_at FROM signals ORDER BY observed_at DESC")
    by_cat = {}
    newest = sigs[0]["observed_at"] if sigs else None
    for s in sigs:
        by_cat[s["category"]] = by_cat.get(s["category"], 0) + 1
    sources = {}
    for row in db.query("SELECT source, COUNT(*) AS n FROM documents GROUP BY source"):
        sources[row["source"]] = {"docs": row["n"]}
    for row in db.query("SELECT * FROM source_cursors"):
        slot = sources.setdefault(row["source"], {})
        slot["last_run"] = row.get("last_run_at")
        slot["fail_count"] = row.get("fail_count")
        slot["last_error"] = row.get("last_error")
        slot["next_due"] = row.get("next_due_at")
    runs = db.query("SELECT run_id, stage, status, started_at FROM runs ORDER BY started_at DESC LIMIT 5")
    docs = db.one("SELECT COUNT(*) AS n, COALESCE(SUM(byte_size),0) AS b FROM documents") or {"n": 0, "b": 0}
    raw_mb = (docs["b"] or 0) / MB
    storage = {"db_mb": 0.0, "raw_mb": round(raw_mb, 1), "docs": docs["n"]}
    quota = raw_quota_check(raw_mb, raw_quota_mb)
    if quota is not None:
        storage["raw_quota"] = quota
    return {
        "accounts": {"total": len(accounts), "by_tier": by_tier, "disqualified": dq},
        "signals": {"total": len(sigs), "by_category": by_cat, "newest": newest},
        "sources": sources,
        "runs": {"last_5": [dict(r) for r in runs]},
        "storage": storage,
    }


def render_status(report: dict) -> str:
    lines = [
        f"accounts {report['accounts']['total']}  disqualified {report['accounts']['disqualified']}",
        f"signals {report['signals']['total']}  newest {report['signals']['newest']}",
        f"docs {report['storage']['docs']}",
    ]
    for key, info in sorted((report.get("sources") or {}).items()):
        lines.append(f"source {key} docs={info.get('docs', 0)} fail={info.get('fail_count', 0)}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# sources.yaml config lint (Task 2, P3 polish roadmap)
#
# A per-source config entry may carry only keys the adapter actually consumes.
# Known keys = the common scheduling set below, plus any class-level attribute
# the adapter itself defines, plus the per-source allowlist. To extend for a
# new source-specific option, add the key to _PER_SOURCE_KNOWN_KEYS for that
# source (or give the adapter a class attribute of the same name).
# ---------------------------------------------------------------------------

_COMMON_KNOWN_KEYS = {"enabled", "cadence_hours", "rate_per_host", "requires"}

_PER_SOURCE_KNOWN_KEYS: dict[str, set[str]] = {
    "google_news": {"serp_keywords"},
    "company_feed": {"blog_feed_url"},
    "appstore_reviews": {"app_store_id"},
    "bbb_profile": {"extra_data"},
    # jobsignals tuning thresholds
    "jobsignals": {"surge_min_roles", "surge_window_days"},
    # consumed by orchestrator.py sec_formd branch (recent_days/exclude/include)
    "sec_formd": {"recent_days", "exclude_pooled_funds", "include_amendments"},
    # reserved host override for the data.sec.gov endpoint
    "sec_edgar": {"host"},
}


def _adapter_known_keys(cls) -> set[str]:
    """Keys consumed by an adapter: common set + its own class attributes."""
    known = set(_COMMON_KNOWN_KEYS)
    known |= {
        name
        for name in dir(cls)
        if not name.startswith("_") and not callable(getattr(cls, name, None))
    }
    return known


def lint_source_config(cfg, sources_table: dict) -> list[tuple[str, str]]:
    """Return (source_key, problem) pairs for unconsumed config keys.

    Pure function: `cfg` is the parsed sources.yaml document, `sources_table`
    maps source key -> adapter class (e.g. src.sources.registry.SOURCES).
    Missing/empty tables lint clean.
    """
    entries = (cfg or {}).get("sources", cfg) if isinstance(cfg, dict) else None
    if not isinstance(entries, dict):
        return []
    problems: list[tuple[str, str]] = []
    for key, entry in sorted(entries.items()):
        if not isinstance(entry, dict):
            continue
        cls = sources_table.get(key)
        if cls is None:
            # Unregistered source: only the common keys can be judged here.
            known = set(_COMMON_KNOWN_KEYS) | _PER_SOURCE_KNOWN_KEYS.get(key, set())
        else:
            known = _adapter_known_keys(cls) | _PER_SOURCE_KNOWN_KEYS.get(key, set())
        for k in sorted(entry):
            if k not in known:
                problems.append((key, f"{key}.{k} not consumed by adapter '{key}'"))
    return problems


def _domain_in_file_refs(node) -> set[str]:
    """Collect every distinct *_in_file list path referenced in a parsed doc.

    Walks the whole document (icp.yaml rules/disqualifiers/any future
    section), so extra or unknown list references are tolerated rather than
    dropped. Only string values count — the same shape load_domain_list
    consumes.
    """
    out: set[str] = set()
    if isinstance(node, dict):
        for key, val in node.items():
            if isinstance(key, str) and key.endswith("_in_file") and isinstance(val, str):
                out.add(val)
            else:
                out |= _domain_in_file_refs(val)
    elif isinstance(node, list):
        for item in node:
            out |= _domain_in_file_refs(item)
    return out


def doctor(config, db, *, check_network: bool = True) -> list[tuple[str, str, str]]:
    out = []

    def add(name, status, detail):
        out.append((name, status, detail))

    add("python", "OK" if sys.version_info >= (3, 12) else "FAIL", sys.version.split()[0])
    try:
        import httpx, lxml, yaml  # noqa: F401
        add("deps", "OK", "httpx lxml yaml")
    except Exception as exc:
        add("deps", "FAIL", str(exc))
    try:
        db.one("SELECT 1")
        add("db", "OK", "reachable")
    except Exception as exc:
        add("db", "FAIL", str(exc))
    try:
        config.load_yaml("signals")
        add("config", "OK", "signals.yaml")
    except Exception as exc:
        add("config", "FAIL", str(exc))
    # sources.yaml config lint: one aggregated check for unconsumed keys.
    try:
        from src.sources.registry import SOURCES as _SOURCES_TABLE

        cfg_doc = config.load_yaml("sources")
        problems = lint_source_config(cfg_doc, _SOURCES_TABLE)
        if problems:
            detail = "; ".join(p for _, p in problems)
            add("config_lint", "WARN", detail)
        else:
            add("config_lint", "OK", "sources.yaml")
    except FileNotFoundError:
        add("config_lint", "OK", "sources.yaml absent")
    except Exception as exc:
        add("config_lint", "WARN", f"lint skipped: {exc}")
    # Optional native antibot engine: WARN (never FAIL) when absent —
    # curl_cffi covers the runtime need; consistent with the
    # pytest.importorskip pattern in tests/test_antibot_engine.py.
    try:
        import signals_antibot  # noqa: F401

        add("antibot_engine", "OK", "native engine available")
    except Exception:
        add("antibot_engine", "WARN", "signals_antibot not built — curl_cffi fallback")
    # Playwright chromium install: WARN with remedy when the browser
    # binary is missing or playwright itself is not installed.
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            exe = p.chromium.executable_path
            if exe and Path(exe).exists():
                add("browser", "OK", str(exe))
            else:
                add("browser", "WARN", "chromium executable not found — run playwright install chromium")
    except Exception:
        add("browser", "WARN", "playwright unavailable — run playwright install chromium")
    if not getattr(config, "contact_email", None):
        add("contact_email", "FAIL", "SIGNALS_CONTACT_EMAIL unset — SEC will 403")
    else:
        add("contact_email", "OK", config.contact_email)
    for label, path in (
        ("linkedin_db", config.external_dbs.linkedin_db),
        ("repvue_db", config.external_dbs.repvue_db),
    ):
        add(label, "OK" if Path(path).exists() else "WARN", path)
    if config.browser.enabled:
        add("playwright", "WARN", "browser.enabled; chromium not probed")
    else:
        add("playwright", "OK", "browser disabled")
    for d in (config.storage.raw_dir, config.storage.export_dir, config.storage.briefs_dir):
        p = Path(d)
        try:
            p.mkdir(parents=True, exist_ok=True)
            add(f"write:{d}", "OK", "writable")
        except Exception as exc:
            add(f"write:{d}", "FAIL", str(exc))
    # Referenced list files (plan T8): parse icp.yaml and WARN for every
    # domain_in_file path that does not exist. Path resolution mirrors
    # src/identity/lists.py load_domain_list (Path(ref): CWD-relative, absolute
    # paths pass through) so the doctor and the ICP loader agree on what is
    # missing — the loader itself fails open on a missing file, so this WARN is
    # the visibility. Pure file reads: safe with check_network=False. A missing
    # or unparseable icp.yaml skips the check gracefully; with no references at
    # all, fall back to the legacy lists-directory existence check.
    lists_dir = Path(config.config_dir) / "lists"
    try:
        icp_doc = config.load_yaml("icp")
    except Exception:
        add("lists", "OK", "icp.yaml not readable — referenced-list check skipped")
    else:
        refs = sorted(_domain_in_file_refs(icp_doc))
        if refs:
            missing = [ref for ref in refs if not Path(ref).exists()]
            if missing:
                add("lists", "WARN", "missing referenced list file(s): " + ", ".join(missing))
            else:
                add("lists", "OK", f"{len(refs)} referenced list file(s) present")
        elif lists_dir.exists():
            add("lists", "OK", str(lists_dir))
        else:
            add("lists", "WARN", str(lists_dir))
    # identity_candidates review queue (plan T2): pure db read — a human
    # backlog is a WARN, never a FAIL.
    try:
        pending = db.one(
            "SELECT COUNT(*) AS n FROM identity_candidates WHERE status = 'pending'"
        )["n"]
        if pending:
            add(
                "identity_candidates_pending",
                "WARN",
                f"{pending} pending identity candidates "
                "(review via sweep --discover / candidates list)",
            )
        else:
            add("identity_candidates_pending", "OK", "no pending identity candidates")
    except Exception as exc:
        add("identity_candidates_pending", "WARN", f"check skipped: {exc}")
    # GKG identity-discovery credentials (plan T13): the gkg_ids waterfall
    # stage is a credential-gated no-op — backend "ekg" (default) needs
    # google-auth (the optional [gkg] extra) plus GOOGLE_APPLICATION_CREDENTIALS
    # (and GKG_PROJECT_ID at call time); backend "kgsearch" needs
    # GOOGLE_KGSEARCH_KEY. Pure env/import probes — safe with check_network=False.
    try:
        backend = getattr(config, "gkg_backend", "ekg") or "ekg"
        if backend == "ekg":
            try:
                import google.auth  # noqa: F401

                have_auth = True
            except Exception:
                have_auth = False
            creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
            if have_auth and creds:
                detail = "ekg: google-auth + GOOGLE_APPLICATION_CREDENTIALS present"
                if not os.environ.get("GKG_PROJECT_ID"):
                    detail += "; GKG_PROJECT_ID unset — EKG calls error until it is set"
                add("gkg_credentials", "OK", detail)
            else:
                missing = []
                if not have_auth:
                    missing.append('google-auth (pip install -e ".[gkg]")')
                if not creds:
                    missing.append("GOOGLE_APPLICATION_CREDENTIALS")
                add(
                    "gkg_credentials",
                    "WARN",
                    "ekg backend unconfigured (missing "
                    + ", ".join(missing)
                    + ") — the GKG identity-discovery stage no-ops until configured",
                )
        elif backend == "kgsearch":
            if os.environ.get("GOOGLE_KGSEARCH_KEY"):
                add("gkg_credentials", "OK", "kgsearch: GOOGLE_KGSEARCH_KEY present")
            else:
                add(
                    "gkg_credentials",
                    "WARN",
                    "kgsearch backend unconfigured (missing GOOGLE_KGSEARCH_KEY) — "
                    "the GKG identity-discovery stage no-ops until configured",
                )
        else:
            add(
                "gkg_credentials",
                "WARN",
                f"unknown gkg_backend {backend!r} — expected 'ekg' or 'kgsearch'; "
                "the GKG stage would be recorded as a stage error",
            )
    except Exception as exc:
        add("gkg_credentials", "WARN", f"check skipped: {exc}")
    if check_network:
        try:
            import httpx

            httpx.Client(timeout=5).head("https://www.sec.gov/", follow_redirects=True)
            add("network", "OK", "sec.gov")
        except Exception as exc:
            add("network", "WARN", str(exc))
    add("disk", "OK", "raw store")
    try:
        raw_mb = (db.one("SELECT COALESCE(SUM(byte_size), 0) AS b FROM documents") or {"b": 0})["b"] / MB
        quota = raw_quota_check(raw_mb, getattr(config.storage, "raw_quota_mb", None))
        if quota is not None:
            add(
                "raw_quota",
                quota["status"],
                f"raw {quota['used_mb']}MB / quota {quota['quota_mb']}MB",
            )
    except Exception as exc:
        add("raw_quota", "WARN", f"quota check skipped: {exc}")
    for _, status, _ in out:
        assert status in {"OK", "WARN", "FAIL"}
    return out
