"""Decay-weighted account scoring with caps and saturation."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import date

from src.core.models import Account, Signal
from src.core.textutil import to_iso_date
from src.signals.calibration import blend_confidence
from src.signals.taxonomy import Taxonomy, UnknownSignalType


@dataclass(frozen=True)
class Contribution:
    signal_id: str
    signal_type: str
    source: str
    base: float
    decay: float
    confidence: float
    value: float
    dropped_reason: str | None = None


@dataclass
class ScoreResult:
    score: float
    raw: float
    contributions: list[Contribution]
    combos: list[dict]
    urgency: int
    icp_multiplier: float

    def to_components_json(self) -> str:
        return json.dumps(
            {
                "score": self.score,
                "raw": self.raw,
                "urgency": self.urgency,
                "icp_multiplier": self.icp_multiplier,
                "combos": self.combos,
                "contributions": [asdict(c) for c in self.contributions],
            },
            sort_keys=True,
        )


def decay_factor(observed_at: str, today: date, half_life_days: int, floor: float) -> float | None:
    """Decay multiplier for an observed date; ``None`` when the date is unknown.

    An unparseable ``observed_at`` must NOT decay to the floor (that would
    silently treat unknown as decades-old); callers decide how to weight an
    unknown date explicitly.
    """
    iso = to_iso_date(observed_at)
    if not iso:
        return None
    observed = date.fromisoformat(iso)
    age = (today - observed).days
    if age <= 0:
        return 1.0
    if half_life_days <= 0:
        return floor
    val = 0.5 ** (age / half_life_days)
    return max(floor, min(1.0, val))


def score_account(
    account: Account,
    signals: list[Signal],
    *,
    taxonomy: Taxonomy,
    cfg: dict,
    today: date,
    combos: list[dict] | None = None,
    calibration_stats: dict | None = None,
) -> ScoreResult:
    floor = float(cfg.get("decay", {}).get("floor", 0.02))
    cap_n = int(cfg.get("caps", {}).get("per_type_max_signals", 3))
    share = float(cfg.get("caps", {}).get("per_source_max_share", 0.5))
    k = float(cfg.get("saturation", {}).get("k", 40.0))
    icp_min = float(cfg.get("icp", {}).get("min_multiplier", 0.0))
    icp_max = float(cfg.get("icp", {}).get("max_multiplier", 2.0))

    contribs: list[Contribution] = []
    kept: list[tuple[Signal, Contribution]] = []

    # 1. drop
    eligible: list[Signal] = []
    for sig in signals:
        if sig.superseded_by:
            contribs.append(_zero(sig, 0, 0, "superseded"))
            continue
        if (sig.confidence or 0) < 0.2:
            contribs.append(_zero(sig, 0, 0, "low_confidence"))
            continue
        try:
            taxonomy.get(sig.signal_type)
        except UnknownSignalType:
            contribs.append(_zero(sig, 0, 0, "unknown_type"))
            continue
        eligible.append(sig)

    # 2. per-type cap
    by_type: dict[str, list[Signal]] = {}
    for sig in eligible:
        by_type.setdefault(sig.signal_type, []).append(sig)
    survivors: list[Signal] = []
    for typ, group in by_type.items():
        # Rerank-aware cap ordering: ties on observed_at are broken by
        # evidence_data["relevance"] (absent -> 0.0, so ordering is unchanged
        # for pre-rerank signals). Descending + stable.
        group.sort(
            key=lambda s: (
                s.observed_at,
                float((s.evidence_data or {}).get("relevance") or 0.0),
            ),
            reverse=True,
        )
        survivors.extend(group[:cap_n])
        for extra in group[cap_n:]:
            spec = taxonomy.get(extra.signal_type)
            decay = decay_factor(extra.observed_at, today, spec.half_life_days, floor)
            if decay is None:
                decay = 1.0  # unknown date: neutral, not floor
            contribs.append(_zero(extra, spec.weight, decay, "per_type_cap"))

    # 3. values
    pending: list[Contribution] = []
    for sig in survivors:
        spec = taxonomy.get(sig.signal_type)
        decay = decay_factor(sig.observed_at, today, spec.half_life_days, floor)
        # Unknown observed_at: neutral decay 1.0 (never silently floor-decay
        # an unknown date to look decades-old).
        if decay is None:
            decay = 1.0
        # Calibration (P1 Task 6): blend confidence with observed per-source
        # hit rates when stats are supplied. None/empty stats -> no-op.
        confidence = blend_confidence(
            float(sig.confidence), sig.source, sig.signal_type, calibration_stats
        )
        value = spec.weight * decay * confidence
        pending.append(
            Contribution(
                signal_id=sig.signal_id,
                signal_type=sig.signal_type,
                source=sig.source,
                base=spec.weight,
                decay=decay,
                confidence=confidence,
                value=value,
            )
        )

    # 4. per-source cap
    total = sum(c.value for c in pending) or 0.0
    if total > 0 and share < 1:
        by_src: dict[str, float] = {}
        for c in pending:
            by_src[c.source] = by_src.get(c.source, 0.0) + c.value
        scaled: list[Contribution] = []
        for c in pending:
            src_sum = by_src[c.source]
            limit = share * total
            if src_sum > limit and src_sum > 0:
                factor = limit / src_sum
                scaled.append(
                    Contribution(
                        signal_id=c.signal_id,
                        signal_type=c.signal_type,
                        source=c.source,
                        base=c.base,
                        decay=c.decay,
                        confidence=c.confidence,
                        value=c.value * factor,
                    )
                )
            else:
                scaled.append(c)
        pending = scaled

    contribs.extend(pending)
    raw_signals = sum(c.value for c in pending)
    fired = list(combos or [])
    bonus = sum(float(c.get("bonus") or 0) for c in fired)
    raw = raw_signals + bonus
    icp = max(icp_min, min(icp_max, float(account.icp_fit if account.icp_fit is not None else 1.0)))
    raw *= icp
    score = round(100 * (1 - math.exp(-raw / k)) if k else 0.0, 1)
    if score > 100:
        score = 100.0
    urgency = max((int(c.get("urgency") or 0) for c in fired), default=0)
    # stable order
    contribs.sort(key=lambda c: (c.dropped_reason or "", c.signal_id))
    return ScoreResult(
        score=score,
        raw=raw,
        contributions=contribs,
        combos=fired,
        urgency=urgency,
        icp_multiplier=icp,
    )


def _zero(sig: Signal, base: float, decay: float, reason: str) -> Contribution:
    return Contribution(
        signal_id=sig.signal_id,
        signal_type=sig.signal_type,
        source=sig.source,
        base=base,
        decay=decay,
        confidence=float(sig.confidence or 0),
        value=0.0,
        dropped_reason=reason,
    )
