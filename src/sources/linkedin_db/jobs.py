"""PURE LinkedIn jobs → leadership_job_open."""

from __future__ import annotations

from datetime import date, timedelta

from src.core.textutil import guess_seniority, to_iso_date
from src.sources.base import SignalCandidate


def linkedin_jobs_to_candidates(rows, domain, *, today: date) -> list[SignalCandidate]:
    out = []
    cutoff = today - timedelta(days=45)
    for r in rows:
        title = r.get("title") or ""
        if guess_seniority(title) not in {"c_level", "vp", "director", "head"}:
            continue
        listed = to_iso_date(r.get("listed_at") or r.get("posted_at"))
        if listed and date.fromisoformat(listed) < cutoff:
            continue
        out.append(
            SignalCandidate(
                signal_type="leadership_job_open",
                observed_at=listed or today.isoformat(),
                natural_key=f"lijob:{r.get('job_id') or title}",
                title=title,
                url=r.get("apply_url") or r.get("url"),
                confidence=0.85,
                evidence_data={"department": r.get("department")},
            )
        )
    return out
