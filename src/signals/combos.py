"""Stacking combo evaluation (the Execution Matrix)."""

from __future__ import annotations

from datetime import date, timedelta

from src.core.config import ConfigError
from src.core.models import Signal
from src.core.textutil import to_iso_date

KNOWN_COND = {"any_type", "within_days", "min_count", "min_confidence"}


def condition_matches(signals: list[Signal], cond: dict, *, today: date) -> list[Signal]:
    unknown = set(cond) - KNOWN_COND
    if unknown:
        raise ConfigError(f"Unknown combo condition key(s): {sorted(unknown)}")
    types = set(cond.get("any_type") or [])
    within = cond.get("within_days")
    min_conf = float(cond.get("min_confidence") or 0.0)
    hits = []
    for sig in signals:
        if types and sig.signal_type not in types:
            continue
        if (sig.confidence or 0) < min_conf:
            continue
        iso = to_iso_date(sig.observed_at)
        if not iso:
            continue
        age = (today - date.fromisoformat(iso)).days
        if within is not None and age > int(within):
            continue
        if age < 0 and within is not None:
            # future still counts as within
            pass
        hits.append(sig)
    min_count = int(cond.get("min_count") or 1)
    if len(hits) < min_count:
        return []
    return hits


def evaluate_combos(signals: list[Signal], combo_defs: list[dict], *, today: date) -> list[dict]:
    fired = []
    for spec in combo_defs:
        all_of = spec.get("all_of") or []
        none_of = spec.get("none_of") or []
        matched_ids: list[str] = []
        ok = True
        for cond in all_of:
            hits = condition_matches(signals, cond, today=today)
            if not hits:
                ok = False
                break
            matched_ids.extend(h.signal_id for h in hits)
        if not ok:
            continue
        blocked = False
        for cond in none_of:
            if condition_matches(signals, cond, today=today):
                blocked = True
                break
        if blocked:
            continue
        fired.append(
            {
                "id": spec["id"],
                "bonus": spec.get("bonus"),
                "urgency": spec.get("urgency"),
                "action": spec.get("action"),
                "matched_signal_ids": list(dict.fromkeys(matched_ids)),
            }
        )
    fired.sort(key=lambda d: int(d.get("urgency") or 0), reverse=True)
    return fired
