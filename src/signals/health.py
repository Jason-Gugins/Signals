"""Account health score: aggregate signal polarity into a -1.0..1.0 float.

Pure module — no taxonomy, config, or DB imports. Polarity is derived from the
signal TYPE NAME via the explicit mapping below so the function stays
dependency-free and trivially testable.

This is distinct from :mod:`src.pipeline.health` (DB/pipeline status
reporting) — that module reports system health; this one scores account
health from observed signals.
"""

from __future__ import annotations

# Signal type name -> polarity sign. Explicit constant; do NOT derive from
# the taxonomy (keeps this module importable without config files).
NEGATIVE_TYPES = frozenset(
    {
        "layoff",
        "earnings_warning",
        "exec_departure",
        "competitor_outage",
    }
)
POSITIVE_TYPES = frozenset(
    {
        "funding_round",
        "exec_hire",
        "product_launch",
        "hiring_surge",
        "ma_acquirer",
        "award",
        "ipo_filing",
        "ipo_pricing",
    }
)

# Default weight per matching signal. Negative types are weighted heavier:
# bad news dominates the score at equal counts (WARN + layoff should gate).
DEFAULT_WEIGHTS: dict[str, float] = {
    "layoff": 1.5,
    "earnings_warning": 1.5,
    "exec_departure": 1.5,
    "competitor_outage": 1.5,
    "funding_round": 1.0,
    "exec_hire": 1.0,
    "product_launch": 1.0,
    "hiring_surge": 1.0,
    "ma_acquirer": 1.0,
    "award": 1.0,
    "ipo_filing": 1.0,
    "ipo_pricing": 1.0,
}

# Safety net for types in the polarity maps but missing from a config file.
FALLBACK_WEIGHT = 1.0


def health_score(
    signals: list,
    *,
    weights: dict | None = None,
) -> tuple[float, list[str]]:
    """Aggregate signal polarity into a normalized health score.

    Args:
        signals: Signal objects with a ``signal_type`` attribute.
        weights: Optional per-type weight override (from config/health.yaml).
            ``None`` uses :data:`DEFAULT_WEIGHTS`; a provided dict is used as
            the complete mapping, with unknown types falling back to
            :data:`FALLBACK_WEIGHT`.

    Returns:
        (score, reasons): score is a float in -1.0..1.0 (0.0 when no signal
        contributes); reasons is a list of short human-readable strings like
        ``"layoff: -1.5"`` for each contributing signal.
    """
    wmap = DEFAULT_WEIGHTS if weights is None else weights
    total = 0.0
    reasons: list[str] = []
    for signal in signals:
        stype = getattr(signal, "signal_type", "") or ""
        if stype in POSITIVE_TYPES:
            sign = 1.0
        elif stype in NEGATIVE_TYPES:
            sign = -1.0
        else:
            continue  # unknown/neutral types contribute nothing
        weight = float(wmap.get(stype, FALLBACK_WEIGHT))
        total += sign * weight
        reasons.append(f"{stype}: {sign * weight:+.1f}")
    # Normalize to -1.0..1.0. The raw sum is unbounded (one row per signal),
    # so scale by a fixed divisor: 3.0 ≈ two heavyweight (negative) signals —
    # the smallest stack that should reach the extremes. Large positive stacks
    # saturate at +1.0; that loss of resolution above the clamp is accepted
    # because the score only feeds a threshold gate (healthy vs gated).
    score = max(-1.0, min(1.0, total / 3.0))
    return score, reasons
