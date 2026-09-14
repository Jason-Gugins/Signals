"""Seller-market relevance profiles.

A market profile describes what the SELLER offers, so the intel flow can
separate "interesting public evidence" from "evidence that matters to this
market". The matching here is deliberately literal: case-insensitive but no
stemming, no fuzzy matching, no embeddings, no LLM, no caching. An unfilled
(near-empty) profile matches nothing on purpose.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from src.core.config import ConfigError

_LIST_KEYS = (
    "buyer_departments",
    "served_problem_phrases",
    "required_vendors",
    "competitor_vendors",
    "relevant_signal_types",
)


@dataclass(frozen=True)
class Offering:
    offering_id: str
    buyer_departments: tuple[str, ...]
    served_problem_phrases: tuple[str, ...]
    required_vendors: tuple[str, ...]
    competitor_vendors: tuple[str, ...]
    relevant_signal_types: tuple[str, ...]


@dataclass(frozen=True)
class MarketProfile:
    profile_id: str
    seller: str
    offerings: tuple[Offering, ...]

    @property
    def is_empty(self) -> bool:
        if not self.offerings:
            return True
        return all(
            not offering.served_problem_phrases and not offering.required_vendors
            for offering in self.offerings
        )


@dataclass(frozen=True)
class RelevanceMatch:
    offering_id: str
    reasons: tuple[str, ...]


def _normalise_terms(value: object) -> tuple[str, ...]:
    """Lowercase, strip, drop empties; absent/None becomes an empty tuple."""
    if not isinstance(value, (list, tuple)):
        return ()
    out: list[str] = []
    for item in value:
        text = str(item).strip().lower()
        if text:
            out.append(text)
    return tuple(out)


def load_market_profile(
    profile_id: str, path: Path = Path("config/markets.yaml")
) -> MarketProfile:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    profiles = data.get("profiles") or {}
    if profile_id not in profiles:
        raise ConfigError(f"unknown market profile: {profile_id}")
    raw = profiles[profile_id] or {}

    raw_offerings = raw.get("offerings", [])
    if raw_offerings is None:
        raw_offerings = []
    if not isinstance(raw_offerings, list):
        raise ConfigError(
            f"market profile {profile_id!r}: 'offerings' must be a list"
        )

    offerings: list[Offering] = []
    seen: set[str] = set()
    for entry in raw_offerings:
        if not isinstance(entry, dict):
            raise ConfigError(
                f"market profile {profile_id!r}: each offering must be a mapping"
            )
        offering_id = str(entry.get("id", "") or "").strip()
        if not offering_id:
            raise ConfigError(
                f"market profile {profile_id!r}: offering is missing an id"
            )
        if offering_id in seen:
            raise ConfigError(
                f"market profile {profile_id!r}: duplicate offering id {offering_id!r}"
            )
        seen.add(offering_id)
        offerings.append(
            Offering(
                offering_id=offering_id,
                buyer_departments=_normalise_terms(entry.get("buyer_departments")),
                served_problem_phrases=_normalise_terms(
                    entry.get("served_problem_phrases")
                ),
                required_vendors=_normalise_terms(entry.get("required_vendors")),
                competitor_vendors=_normalise_terms(entry.get("competitor_vendors")),
                relevant_signal_types=_normalise_terms(
                    entry.get("relevant_signal_types")
                ),
            )
        )

    seller = raw.get("seller", "")
    seller = "" if seller is None else str(seller)

    return MarketProfile(
        profile_id=profile_id,
        seller=seller,
        offerings=tuple(offerings),
    )


def match_relevance(
    *,
    text: str,
    department: str | None,
    signal_type: str,
    vendors: list[str] | tuple[str, ...] | None,
    profile: MarketProfile,
) -> RelevanceMatch | None:
    if profile.is_empty:
        return None

    haystack = (text or "").casefold()
    signal = (signal_type or "").casefold()
    dept = (department or "").casefold()
    vendor_list = vendors or ()

    for offering in profile.offerings:
        if signal not in offering.relevant_signal_types:
            continue

        reasons: list[str] = []
        for phrase in offering.served_problem_phrases:
            if phrase in haystack:
                reasons.append(f"phrase:{phrase}")
        for vendor in vendor_list:
            folded = str(vendor).casefold()
            if folded in offering.required_vendors:
                reasons.append(f"vendor:{folded}")

        if not reasons:
            continue

        if dept and offering.buyer_departments:
            if dept not in offering.buyer_departments:
                continue
            reasons.append(f"department:{department}")

        return RelevanceMatch(offering_id=offering.offering_id, reasons=tuple(reasons))

    return None