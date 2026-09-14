"""Truthful one-row-per-source coverage for a collection run.

Coverage enumerates the CONFIG registry in ``config/sources.yaml`` — NOT the
enabled subset. Every configured key gets exactly one row, so a source that is
disabled in config, deliberately opted out, unregistered, gated by the ATS
vendor check, or missing a required account field is *visible* in the report
rather than silently missing from it.

Presence of a row NEVER implies the source ran. Rows built from the runner's
per-source outcomes carry a terminal run status (ran_data, ran_empty, failed,
cadence_not_due, backed_off, dry_run_planned, ...). Rows built by classifying
the config instead carry a classification status (opt_in, disabled_by_config,
not_registered, ineligible_ats_vendor, missing_requires, not_selected) and
zeroed counters — a source that did not run is never reported as having run,
and a source that is merely eligible is reported as ``not_selected``, not as
having produced an empty result. Runner truth always wins over classification.

Callers (the intel CLI and the dossier) should share
:func:`coverage_summary` so totals mean the same thing everywhere.
"""

from __future__ import annotations

from src.pipeline.runner import _missing_field
from src.sources.registry import (
    SOURCES,
    _CAREERS_FALLBACK_KEY,
    _ats_vendor_ok,
)

_COUNTERS = ("tasks", "fetched", "cached", "failed", "candidates", "signals_new")


def _row(source: str, key: str, status: str, reason: str | None) -> dict:
    row = {"source": source, "key": key, "status": status, "reason": reason}
    for counter in _COUNTERS:
        row[counter] = 0
    return row


def _row_from_outcome(outcome: dict) -> dict:
    """Copy a runner outcome row verbatim (its own key is authoritative)."""
    row = {
        "source": outcome["source"],
        "key": outcome["key"],
        "status": outcome["status"],
        "reason": outcome.get("reason"),
    }
    for counter in _COUNTERS:
        row[counter] = outcome.get(counter, 0)
    return row


def _ats_reason(adapter_key: str, account) -> str:
    if adapter_key == _CAREERS_FALLBACK_KEY:
        return "careers fallback requires empty ats_vendor and ats_token"
    return (
        f"ats vendor gate: adapter {adapter_key} does not match "
        f"ats_vendor={account.ats_vendor!r}"
    )


def build_coverage(
    *,
    config,
    account,
    outcomes,
    opt_in_flags=None,
    requested=None,
) -> list[dict]:
    """One row per configured source key, in the YAML's sorted key order.

    ``outcomes`` is the runner's per-source outcome list (may be empty).
    ``requested`` is the set of source keys the caller explicitly asked for;
    it is used only to annotate opt-in rows. ``opt_in_flags`` maps a source key
    to the CLI flag text that would enable it.
    """
    opt_ins = dict(opt_in_flags or {})
    requested = set(requested or ())
    by_source: dict[str, dict] = {}
    for outcome in outcomes or ():
        by_source.setdefault(outcome["source"], outcome)

    table = config.load_yaml("sources")
    entries = table.get("sources", table)
    defaults = table.get("defaults") or {}

    rows: list[dict] = []
    for source_key, entry in sorted((entries or {}).items()):
        if not isinstance(entry, dict):
            continue

        # 1. Runner truth always wins.
        outcome = by_source.get(source_key)
        if outcome is not None:
            rows.append(_row_from_outcome(outcome))
            continue

        # 2. Configured off: opted-in (a flag exists) or plain disabled.
        if not entry.get("enabled", defaults.get("enabled", True)):
            if source_key in opt_ins:
                note = " (requested)" if source_key in requested else ""
                rows.append(
                    _row(
                        source_key,
                        account.domain,
                        "opt_in",
                        f"disabled in config; opt in with {opt_ins[source_key]}{note}",
                    )
                )
            else:
                rows.append(
                    _row(
                        source_key,
                        account.domain,
                        "disabled_by_config",
                        "disabled in config (enabled: false)",
                    )
                )
            continue

        # 3. No adapter for this configured key.
        adapter = SOURCES.get(source_key)
        if adapter is None:
            rows.append(
                _row(
                    source_key,
                    account.domain,
                    "not_registered",
                    f"adapter not in source registry: {source_key}",
                )
            )
            continue

        # 4. ATS vendor gate.
        if not _ats_vendor_ok(adapter, account):
            rows.append(
                _row(
                    source_key,
                    account.domain,
                    "ineligible_ats_vendor",
                    _ats_reason(adapter.key, account),
                )
            )
            continue

        # 5. A required account field is missing.
        field_name = _missing_field(adapter, account)
        if field_name:
            rows.append(
                _row(
                    source_key,
                    account.domain,
                    "missing_requires",
                    f"missing required account field: {field_name}",
                )
            )
            continue

        # 6. Eligible, but absent from this run — do NOT guess that it ran.
        rows.append(
            _row(
                source_key,
                account.domain,
                "not_selected",
                "eligible but not selected for this run",
            )
        )

    return rows


def coverage_summary(rows) -> dict[str, int]:
    """Count coverage rows by status (one shared definition for CLI + dossier)."""
    summary: dict[str, int] = {}
    for row in rows:
        summary[row["status"]] = summary.get(row["status"], 0) + 1
    return summary
