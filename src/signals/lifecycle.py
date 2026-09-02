"""Superseded-signal lifecycle: per-type expiry windows (soft filter, no deletion).

A signal type may declare an optional ``supersede_days`` key in
``config/signals.yaml``. When present, a signal of that type whose
``observed_at`` is strictly older than ``supersede_days`` is considered
superseded and is excluded from scoring/tiering. Absent key = no expiry
(existing behavior unchanged). Expired signals are never deleted or
mutated — they are soft-filtered at scoring time only.
"""

from __future__ import annotations

from datetime import date

from src.core.models import Signal


def _parse_observed_date(value) -> date | None:
    """Parse an observed_at value (ISO date or datetime string) to a date; None on failure."""
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except (ValueError, TypeError):
        return None


def is_superseded(
    signal: Signal, *, today: date, supersede_days_by_type: dict[str, int]
) -> bool:
    """True when the signal's type has a supersede window AND the signal is strictly past it.

    Never expires on bad data: unparseable/absent observed_at -> False.
    """
    window = supersede_days_by_type.get(signal.signal_type)
    if window is None:
        return False
    observed = _parse_observed_date(signal.observed_at)
    if observed is None:
        return False
    return (today - observed).days > window


def partition_signals(
    signals, *, today: date, supersede_days_by_type: dict[str, int]
) -> tuple[list[Signal], list[Signal]]:
    """Split signals into (active, expired). Order within each list is preserved."""
    active: list[Signal] = []
    expired: list[Signal] = []
    for sig in signals:
        if is_superseded(sig, today=today, supersede_days_by_type=supersede_days_by_type):
            expired.append(sig)
        else:
            active.append(sig)
    return active, expired


def load_supersede_map(signals_cfg: dict) -> dict[str, int]:
    """Extract the per-type supersede_days map from a parsed config/signals.yaml dict."""
    types = (signals_cfg or {}).get("types") or {}
    return {
        name: int(spec["supersede_days"])
        for name, spec in types.items()
        if isinstance(spec, dict) and spec.get("supersede_days") is not None
    }
