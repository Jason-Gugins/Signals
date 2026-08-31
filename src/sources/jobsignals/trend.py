"""Hiring-velocity trend signal (PURE) + per-domain open-role stats state.

``hiring_trend_signal`` compares this cycle's open-role count for a domain
against the previous cycle's stored stats and emits an existing
``hiring_surge`` candidate only when the increase crosses the configured
percentage threshold (e.g. "open roles up 40% in 30d"). Decreases and
deltas below threshold -> ``None``.

The per-domain stats live in a small JSON state file (default
``data/jobsignals/stats.json``) keyed by domain; the runner loads it after
harvesting jobs, computes the current open-role count, emits the signal,
then saves the new stats. This module is *deliberately* pure: the file I/O
helpers are separate functions, and the signal function itself does no I/O
and reads no clock — ``today`` is injected.
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

from src.sources.base import SignalCandidate

DEFAULT_STATS_PATH = "data/jobsignals/stats.json"
DEFAULT_MIN_DELTA_PCT = 25.0


def stats_key(domain: str) -> str:
    """State-file key for a domain's open-role stats."""
    return domain


def load_stats(path: str | Path = DEFAULT_STATS_PATH) -> dict:
    """Load the per-domain stats state; tolerant of missing/corrupt files."""
    try:
        p = Path(path)
        if not p.exists():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_stats(stats: dict, path: str | Path = DEFAULT_STATS_PATH) -> None:
    """Persist the per-domain stats state (atomic: temp file + os.replace)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(stats, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


def compute_stats(jobs: list) -> dict:
    """Aggregate harvested job posts into {'count': <open roles>}.

    Jobs passed in are assumed already filtered to open roles by the
    harvest path (``closed_at IS NULL`` on upsert), so the count is simply
    the number of posts seen this cycle for the domain.
    """
    return {"count": len(jobs)}


def hiring_trend_signal(
    domain: str,
    prev: dict | None,
    curr: dict,
    *,
    today: date | str,
    min_delta_pct: float = DEFAULT_MIN_DELTA_PCT,
) -> SignalCandidate | None:
    """Emit a ``hiring_surge`` candidate when open roles grow past threshold.

    ``prev``/``curr`` are ``{"count": int}`` stat dicts (``prev`` is ``None``
    on the first observed cycle -> no signal — a baseline must exist before
    a velocity delta is meaningful). Emits only on *increases*:
    ``delta_pct >= min_delta_pct`` where ``delta_pct`` is the cycle-over-cycle
    percentage growth in open roles. Otherwise ``None``.
    """
    if not prev:
        return None
    today_str = today.isoformat() if isinstance(today, date) else str(today)
    prev_count = int(prev.get("count") or 0)
    curr_count = int(curr.get("count") or 0)
    if prev_count <= 0:
        return None  # no baseline to compute a percentage against
    count_delta = curr_count - prev_count
    delta_pct = (count_delta / prev_count) * 100.0
    if delta_pct < min_delta_pct:
        return None
    return SignalCandidate(
        signal_type="hiring_surge",
        observed_at=today_str,
        natural_key=f"hrtrend:{domain}:{today_str}",
        title=f"Hiring velocity up at {domain}",
        summary=(
            f"{domain} open roles: {prev_count} -> {curr_count} "
            f"(Δ{count_delta:+d}, {delta_pct:+.1f}% cycle-over-cycle)"
        ),
        confidence=0.7,
        evidence_data={
            "domain": domain,
            "prev": dict(prev),
            "current": dict(curr),
            "count_delta": count_delta,
            "delta_pct": round(delta_pct, 2),
            "thresholds": {"min_delta_pct": min_delta_pct},
        },
    )
