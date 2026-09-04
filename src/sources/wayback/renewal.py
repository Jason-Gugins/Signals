"""Renewal-window estimator from first-seen tech dates. PURE."""

from __future__ import annotations

from datetime import date, timedelta

from src.sources.base import SignalCandidate
from src.sources.techstack.fingerprint import TechMatch


def estimate_first_seen(vendor: str, dated_matches: list[tuple[str, list[TechMatch]]]) -> str | None:
    for iso, matches in dated_matches:
        if any(m.vendor == vendor for m in matches):
            return iso
    return None


def renewal_candidates(domain: str, tech_rows: list[dict], *, contract_years: dict[str, int], default_years: int = 1, today: date, lead_days: tuple[int, int] = (30, 120)) -> list[SignalCandidate]:
    lo, hi = lead_days
    out = []
    for row in tech_rows:
        first = row.get("first_seen_at")
        if not first:
            continue
        start = date.fromisoformat(first[:10])
        years = contract_years.get(row["vendor"], default_years)
        k = 1
        while True:
            try:
                renewal = date(start.year + years * k, start.month, start.day)
            except ValueError:  # Feb 29 first_seen on a non-leap renewal year
                renewal = date(start.year + years * k, start.month, 28)
            delta = (renewal - today).days
            if renewal < today - timedelta(days=400):
                k += 1
                continue
            if delta > hi + 366:
                break
            if lo <= delta <= hi:
                out.append(
                    SignalCandidate(
                        signal_type="renewal_window",
                        observed_at=today.isoformat(),
                        natural_key=f"renewal:{row['vendor']}:{renewal.isoformat()}",
                        title=f"{row['vendor']} renewal ~{renewal}",
                        confidence=0.5,
                        evidence_data={
                            "competitor": row["vendor"],
                            "first_seen": first,
                            "renewal_estimate": renewal.isoformat(),
                            "basis": "wayback_first_seen",
                            "confidence_note": "estimated, not contractual",
                        },
                    )
                )
            k += 1
            if k > 8:
                break
    return out
