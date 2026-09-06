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
import re
from datetime import date
from pathlib import Path

from src.sources.base import SignalCandidate

DEFAULT_STATS_PATH = "data/marketplace/stats.json"
DEFAULT_MIN_COUNT_DELTA = 5
DEFAULT_MIN_RATING_DELTA = 0.5

_SIZE_BAND_RE = re.compile(r"^\s*(\d+)\s*(?:-|–|—|to)\s*(\d+)\s*$", re.IGNORECASE)
_SINGLE_NUM_RE = re.compile(r"^\s*(\d+)\s*$")


def size_midpoint(size: str | None) -> int | None:
    """Midpoint of a marketplace company-size band like ``"51-200"``.

    Commas are ignored (``"1,001-5,000"``); a lone number counts as both
    bounds (``"200"`` -> 200). Returns ``None`` for anything without two
    numeric bounds (``"10,000+"``, ``"Enterprise"``, empty, ``None``).
    Pure: no I/O, no clock.
    """
    if size is None:
        return None
    text = str(size).replace(",", "")
    m = _SIZE_BAND_RE.match(text)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        if hi < lo:
            lo, hi = hi, lo
        return (lo + hi) // 2
    m = _SINGLE_NUM_RE.match(text)
    if m:
        return int(m.group(1))
    return None


def reviewer_icp_matches(
    title: str | None,
    size: str | None,
    target_titles: list[str],
    size_band: tuple[int, int] | None,
) -> bool:
    """True when a marketplace reviewer looks like an ICP champion contact.

    Title match: casefolded SUBSTRING in BOTH directions against any target
    ("VP Sales & Marketing" matches "VP Sales"; "head of revenue operations"
    matches "Head of Revenue"). When ``size_band`` is given, the reviewer's
    ``reviewer_company_size`` token must also parse to a midpoint inside the
    band — a missing/unparseable size then never matches (conservative).
    With ``size_band=None`` the title alone decides. Blank/None titles and
    empty target lists never match. Pure: no I/O, no clock.
    """
    t = (title or "").strip().casefold()
    if not t or not target_titles:
        return False
    hit = any(
        (needle := str(target).strip().casefold())
        and (needle in t or t in needle)
        for target in target_titles
    )
    if not hit:
        return False
    if size_band is None:
        return True
    mid = size_midpoint(size)
    if mid is None:
        return False
    lo, hi = size_band
    return lo <= mid <= hi


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
