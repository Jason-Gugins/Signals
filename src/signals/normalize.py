"""Normalize SignalCandidates into validated Signals with stable IDs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from src.core.models import Account, Signal
from src.core.textutil import stable_id, to_iso_date
from src.signals.taxonomy import Taxonomy, UnknownSignalType


class InvalidSignal(ValueError):
    """A candidate cannot become a Signal."""


@dataclass
class SignalCandidate:
    signal_type: str
    observed_at: str
    natural_key: str
    title: Optional[str] = None
    summary: Optional[str] = None
    url: Optional[str] = None
    confidence: float = 0.8
    person_key: Optional[str] = None
    evidence_data: dict = field(default_factory=dict)
    domain_override: Optional[str] = None


def make_signal_id(domain: str, signal_type: str, natural_key: str) -> str:
    """stable_id(domain, signal_type, natural_key, length=16). Case-normalized."""
    return stable_id(domain.casefold(), signal_type.casefold(), natural_key.casefold(), length=16)


def normalize_candidate(
    cand: SignalCandidate,
    *,
    account: Account,
    source: str,
    taxonomy: Taxonomy,
    now: str,
    raw_ref: str | None = None,
) -> Signal:
    try:
        spec = taxonomy.get(cand.signal_type)
    except UnknownSignalType as exc:
        raise InvalidSignal("unknown_signal_type") from exc
    observed = to_iso_date(cand.observed_at)
    if not observed:
        raise InvalidSignal("missing_or_unparseable_observed_at")
    today = to_iso_date(now)
    if today:
        future_limit = date.fromisoformat(today) + timedelta(days=3)
        if date.fromisoformat(observed) > future_limit:
            raise InvalidSignal("observed_at_in_future")
    conf = cand.confidence
    if conf is None:
        conf = 0.8
    conf = max(0.0, min(1.0, float(conf)))
    domain = cand.domain_override or account.domain
    return Signal(
        signal_id=make_signal_id(domain, cand.signal_type, cand.natural_key),
        domain=domain,
        signal_type=spec.key,
        category=spec.category,
        origin=spec.origin,
        catalyst=spec.catalyst,
        polarity=spec.polarity,
        observed_at=observed,
        source=source,
        degree=spec.degree,
        person_key=cand.person_key,
        title=cand.title,
        summary=cand.summary,
        evidence_data=dict(cand.evidence_data or {}),
        url=cand.url,
        confidence=conf,
        first_seen_at=now,
        last_seen_at=now,
        raw_ref=raw_ref,
    )


def normalize_batch(
    cands,
    *,
    account: Account,
    source: str,
    taxonomy: Taxonomy,
    now: str,
    raw_ref: str | None = None,
) -> tuple[list[Signal], list[tuple[SignalCandidate, str]]]:
    valid: list[Signal] = []
    rejected: list[tuple[SignalCandidate, str]] = []
    for cand in cands:
        try:
            valid.append(
                normalize_candidate(
                    cand,
                    account=account,
                    source=source,
                    taxonomy=taxonomy,
                    now=now,
                    raw_ref=raw_ref,
                )
            )
        except InvalidSignal as exc:
            rejected.append((cand, str(exc)))
    return valid, rejected
