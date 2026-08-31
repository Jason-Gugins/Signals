"""Confidence calibration from observed per-source hit rates (P1 Task 6, v1).

YAGNI scope: a `calibration` table (source, signal_type, samples, hits) is
populated by the P2 backtesting loop; until it has data, blending is a strict
no-op and scoring behavior is unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.core.db import Database

MIN_SAMPLES = 30


def blend_confidence(
    base: float,
    source: str,
    signal_type: str,
    stats: dict | None,
    *,
    min_samples: int = MIN_SAMPLES,
) -> float:
    """Blend `base` confidence with the observed hit rate for (source, signal_type).

    No stats, unknown key, or samples < min_samples -> base unchanged.
    Otherwise: base * (0.5 + 0.5 * hits/samples), clamped to [0.05, 0.95].
    """
    if not stats:
        return base
    entry = stats.get((source, signal_type))
    if not entry:
        return base
    samples = int(entry.get("samples") or 0)
    if samples < min_samples:
        return base
    hits = int(entry.get("hits") or 0)
    rate = hits / samples
    return max(0.05, min(0.95, base * (0.5 + 0.5 * rate)))


def load_stats(db: "Database") -> dict:
    """Load calibration stats keyed by (source, signal_type).

    Returns {} when the table is missing or empty (zero-regression path).
    """
    try:
        rows = db.query(
            "SELECT source, signal_type, samples, hits FROM calibration"
        )
    except Exception:
        return {}
    return {
        (r["source"], r["signal_type"]): {"samples": r["samples"], "hits": r["hits"]}
        for r in rows
    }
