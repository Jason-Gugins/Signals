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
    return (today - date.fromisoformat(iso)).days


def newest_primary(signals: list[Signal], *, taxonomy: Taxonomy) -> Signal | None:
    primary = taxonomy.primary_types()
    cands = []
    for s in signals:
        if s.signal_type in primary:
            cands.append(s)
    if not cands:
        return None
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
        if age is not None and age <= active_d:
            return "active"
        if age is not None and age <= opening_d:
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
        if a is None:
            continue
        spec = None
        try:
            spec = taxonomy.get(s.signal_type)
        except Exception:
            continue
        ages.append((s, a, spec))

    prim_int_30 = [s for s, a, spec in ages if s.signal_type in primary and spec.origin == "internal" and a <= 30]
    prim_int_90 = [s for s, a, spec in ages if s.signal_type in primary and spec.origin == "internal" and a <= 90]
    ext_90 = [s for s, a, spec in ages if spec.origin == "external" and a <= 90]
    only_ext_or_d3 = ages and all(spec.origin == "external" or spec.degree == 3 for _, _, spec in ages)

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
