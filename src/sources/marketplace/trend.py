"""Marketplace review-velocity trend signal (PURE) + per-slug stats state.

``review_trend_signal`` compares this cycle's review count / average rating
against the previous cycle's stored stats and emits a
``marketplace_review_trend`` candidate only when a delta crosses the
configured thresholds. No deltas below threshold -> ``None``.

The per-slug stats live in a small JSON state file (default
``data/marketplace/stats.json``) keyed by ``"{source}:{slug}"``; the runner
loads it, computes current stats after harvesting reviews, emits the signal,
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

DEFAULT_STATS_PATH = "data/marketplace/stats.json"
DEFAULT_MIN_COUNT_DELTA = 5
DEFAULT_MIN_RATING_DELTA = 0.5


def stats_key(source: str, product_slug: str) -> str:
    """State-file key for a (source, slug) pair."""
    return f"{source}:{product_slug}"


def load_stats(path: str | Path = DEFAULT_STATS_PATH) -> dict:
    """Load the per-slug stats state; tolerant of missing/corrupt files."""
    try:
        p = Path(path)
        if not p.exists():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_stats(stats: dict, path: str | Path = DEFAULT_STATS_PATH) -> None:
    """Persist the per-slug stats state (atomic: temp file + os.replace)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(stats, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


def compute_stats(reviews: list) -> dict:
    """Aggregate review objects (rating attr) into {'count', 'avg_rating'}."""
    count = len(reviews)
    ratings = [float(r.rating) for r in reviews if r.rating is not None]
    avg = round(sum(ratings) / len(ratings), 2) if ratings else 0.0
    return {"count": count, "avg_rating": avg}


def review_trend_signal(
    product_slug: str,
    source: str,
    prev: dict | None,
    curr: dict,
    *,
    domain: str,
    today: date | str,
    min_count_delta: int = DEFAULT_MIN_COUNT_DELTA,
    min_rating_delta: float = DEFAULT_MIN_RATING_DELTA,
) -> SignalCandidate | None:
    """Emit a ``marketplace_review_trend`` candidate when deltas cross thresholds.

    ``prev``/``curr`` are ``{"count": int, "avg_rating": float}`` stat dicts
    (``prev`` is ``None`` on the first observed cycle -> no signal).
    Emits only when ``abs(count_delta) >= min_count_delta`` OR
    ``abs(rating_delta) >= min_rating_delta``. Otherwise ``None``.
    """
    if not prev:
        return None
    today_str = today.isoformat() if isinstance(today, date) else str(today)
    count_delta = int(curr["count"]) - int(prev["count"])
    rating_delta = float(curr["avg_rating"]) - float(prev["avg_rating"])
    if abs(count_delta) < min_count_delta and abs(rating_delta) < min_rating_delta:
        return None
    return SignalCandidate(
        signal_type="marketplace_review_trend",
        observed_at=today_str,
        natural_key=f"rvtrend:{source}:{product_slug}:{today_str}",
        title=f"Review velocity shift on {source} for {product_slug}",
        summary=(
            f"{source} reviews for {product_slug}: {prev['count']} -> "
            f"{curr['count']} (Δ{count_delta:+d}), avg rating "
            f"{prev['avg_rating']} -> {curr['avg_rating']} (Δ{rating_delta:+.2f})"
        ),
        url=f"https://www.{source}.com/products/{product_slug}/reviews",
        confidence=0.7,
        evidence_data={
            "product_slug": product_slug,
            "source": source,
            "prev": dict(prev),
            "current": dict(curr),
            "count_delta": count_delta,
            "rating_delta": round(rating_delta, 4),
            "thresholds": {
                "min_count_delta": min_count_delta,
                "min_rating_delta": min_rating_delta,
            },
        },
    )
