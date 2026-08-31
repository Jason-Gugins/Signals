"""HPP tiering and buying-window state machine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from src.core.models import Signal
from src.core.textutil import to_iso_date
from src.signals.score import ScoreResult
from src.signals.taxonomy import Taxonomy


@dataclass(frozen=True)
class TierResult:
    tier: int
    buying_window: str
    rationale: str


def _age(sig: Signal, today: date) -> int | None:
    iso = to_iso_date(sig.observed_at)
    if not iso:
        return None
    if len(iso) < 10:
        return None
    try:
        observed = date.fromisoformat(iso[:10])
    except ValueError:
        return None
    return (today - observed).days


_TODAY_SENTINEL = date.max


def newest_primary(signals: list[Signal], *, taxonomy: Taxonomy) -> Signal | None:
    """Newest primary signal — preferring candidates with a KNOWN age.

    Unknown observed_at is neither fresh nor ancient: known-age candidates
    win; the raw-string sort is only a fallback when NO candidate has a
    parseable date.
    """
    primary = taxonomy.primary_types()
    cands = [s for s in signals if s.signal_type in primary]
    if not cands:
        return None
    known = [(s, _age(s, date.max)) for s in cands if _age(s, date.max) is not None]
    if known:
        known.sort(key=lambda pair: pair[1])
        return known[0][0]
    cands.sort(key=lambda s: s.observed_at, reverse=True)
    return cands[0]


def buying_window(signals: list[Signal], *, taxonomy: Taxonomy, cfg: dict, today: date) -> str:
    bw = cfg.get("buying_window") or {}
    active_d = int(bw.get("active_days", 30))
    opening_d = int(bw.get("opening_days", 90))
    developing_d = int(bw.get("developing_days", 180))
    prim = newest_primary(signals, taxonomy=taxonomy)
    if prim is not None:
        age = _age(prim, today)
        if age is not None:
            # Unknown observed_at is window-neutral: it must NOT behave like
            # the freshest signal (age 0). Fall through to the ages-based
            # logic below, where unknown = not newer than known evidence.
            if age <= active_d:
                return "active"
            if age <= opening_d:
                return "opening"
    ages = [a for a in (_age(s, today) for s in signals) if a is not None]
    if ages and min(ages) <= developing_d:
        return "developing"
    return "dormant"


def assign_tier(
    signals: list[Signal],
    result: ScoreResult,
    *,
    taxonomy: Taxonomy,
    cfg: dict,
    today: date,
) -> TierResult:
    window = buying_window(signals, taxonomy=taxonomy, cfg=cfg, today=today)
    tiers = cfg.get("tiers") or {}
    t1 = tiers.get("tier1") or {}
    t2 = tiers.get("tier2") or {}
    t3 = tiers.get("tier3") or {}
    if not signals:
        return TierResult(4, "dormant", "Tier 4: no signals.")

    primary = taxonomy.primary_types()
    ages = []
    for s in signals:
        a = _age(s, today)
        known = a is not None
        if a is None:
            # Unknown observed_at: neutral-fresh (age 0) for score/decay
            # parity, but EXCLUDED from the prim_int_30/90 recency lists
            # below — an unknown date must not count as a *recent* trigger
            # for tier purposes (window-neutral; score.py handles it).
            a = 0
        spec = None
        try:
            spec = taxonomy.get(s.signal_type)
        except Exception:
            continue
        ages.append((s, a, spec, known))

    prim_int_30 = [
        s for s, a, spec, known in ages
        if s.signal_type in primary and spec.origin == "internal" and a <= 30 and known
    ]
    prim_int_90 = [
        s for s, a, spec, known in ages
        if s.signal_type in primary and spec.origin == "internal" and a <= 90 and known
    ]
    ext_90 = [s for s, a, spec, known in ages if spec.origin == "external" and a <= 90]
    only_ext_or_d3 = ages and all(spec.origin == "external" or spec.degree == 3 for _, _, spec, _ in ages)

    if result.urgency >= int(t1.get("or_urgency", 8)):
        return TierResult(1, window, f"Tier 1: combo urgency {result.urgency}.")
    if result.score >= float(t1.get("min_score", 70)):
        return TierResult(1, window, f"Tier 1: score {result.score} ≥ {t1.get('min_score', 70)}.")
    if prim_int_30 and ext_90:
        p = prim_int_30[0]
        e = ext_90[0]
        pa = _age(p, today)
        ea = _age(e, today)
        return TierResult(
            1,
            window,
            f"Tier 1: primary internal trigger {p.signal_type} {pa}d ago + external trigger {e.signal_type} {ea}d ago",
        )
    if result.score >= float(t2.get("min_score", 45)):
        return TierResult(2, window, f"Tier 2: score {result.score}.")
    if prim_int_90:
        p = prim_int_90[0]
        return TierResult(2, window, f"Tier 2: primary internal trigger {p.signal_type} {_age(p, today)}d ago.")
    if result.score >= float(t3.get("min_score", 20)):
        return TierResult(3, window, f"Tier 3: score {result.score}.")
    if only_ext_or_d3:
        return TierResult(3, window, "Tier 3: only external or degree-3 signals present.")
    return TierResult(4, window, "Tier 4: no qualifying recent trigger.")
