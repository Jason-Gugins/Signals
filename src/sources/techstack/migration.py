"""Cycle diff -> replacement (migration) inference. PURE.

A migration is a REMOVED vendor and an ADDED vendor that share a fingerprint
category, observed in the same cycle. tech_migration_mentioned is w24 primary
in the taxonomy because a category swap implies a funded replatforming project
with a known vendor-out. Pure: no I/O, no clock reads (today is injected).
"""
from __future__ import annotations

from typing import Callable, Iterable

from src.sources.base import SignalCandidate


def infer_migrations(
    added: Iterable[str],
    removed: Iterable[str],
    category_of: Callable[[str], set],
    *,
    domain: str,
    today: str,
    flap_guard: set[str] | None = None,
) -> list[SignalCandidate]:
    guard = flap_guard or set()
    added_set = sorted(a for a in added if a not in guard)
    out: list[SignalCandidate] = []
    for gone in sorted(r for r in removed if r not in guard):
        cats = category_of(gone)
        if not cats:
            continue
        match = next((a for a in added_set if cats & category_of(a)), None)
        if match:
            out.append(
                SignalCandidate(
                    "tech_migration_mentioned",
                    today,
                    f"techmig:{domain}:{gone}:{match}:{today[:7]}",
                    title=f"{gone} -> {match}",
                    confidence=0.65,
                    evidence_data={
                        "from": gone,
                        "to": match,
                        "inferred": True,
                        "categories": sorted(cats & category_of(match)),
                    },
                )
            )
    return out
