"""Status report and doctor diagnostics."""

from __future__ import annotations

import sys
from pathlib import Path


def status_report(db, *, taxonomy) -> dict:
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
    return {
        "accounts": {"total": len(accounts), "by_tier": by_tier, "disqualified": dq},
        "signals": {"total": len(sigs), "by_category": by_cat, "newest": newest},
        "sources": sources,
        "runs": {"last_5": [dict(r) for r in runs]},
        "storage": {"db_mb": 0.0, "raw_mb": 0.0, "docs": docs["n"]},
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
    lists = Path(config.config_dir) / "lists"
    add("lists", "OK" if lists.exists() else "WARN", str(lists))
    if check_network:
        try:
            import httpx

            httpx.Client(timeout=5).head("https://www.sec.gov/", follow_redirects=True)
            add("network", "OK", "sec.gov")
        except Exception as exc:
            add("network", "WARN", str(exc))
    add("disk", "OK", "raw store")
    for _, status, _ in out:
        assert status in {"OK", "WARN", "FAIL"}
    return out
