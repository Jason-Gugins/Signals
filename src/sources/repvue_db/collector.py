"""PURE RepVue row → stagnation / hiring support signals."""

from __future__ import annotations

from datetime import date

from src.sources.base import SignalCandidate


def repvue_to_candidates(row: dict, prior: dict | None, *, today: date) -> list[SignalCandidate]:
    out = []
    if prior:
        score = row.get("repvue_score")
        prev = prior.get("repvue_score")
        if score is not None and prev is not None and (prev - score) >= 5:
            out.append(
                SignalCandidate(
                    signal_type="stagnation",
                    observed_at=today.isoformat(),
                    natural_key=f"rvscore:{row.get('domain')}:{today}",
                    title="RepVue score drop",
                    confidence=0.5,
                    evidence_data={"from": prev, "to": score},
                )
            )
        quota = row.get("quota_attainment")
        pq = prior.get("quota_attainment")
        if quota is not None and pq is not None and (pq - quota) >= 0.10:
            out.append(
                SignalCandidate(
                    signal_type="stagnation",
                    observed_at=today.isoformat(),
                    natural_key=f"rvquota:{row.get('domain')}:{today}",
                    title="Quota attainment drop",
                    confidence=0.6,
                    evidence_data={"from": pq, "to": quota},
                )
            )
        if prior.get("hiring") in (0, False, None) and row.get("hiring") in (1, True):
            out.append(
                SignalCandidate(
                    signal_type="hiring_surge",
                    observed_at=today.isoformat(),
                    natural_key=f"rvhire:{row.get('domain')}:{today}",
                    title="RepVue hiring flag on",
                    confidence=0.4,
                    evidence_data={},
                )
            )
    return out
