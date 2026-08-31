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
        raw_listed = r.get("listed_at") or r.get("posted_at")
        evidence = {"department": r.get("department")}
        if listed is None and raw_listed:
            # Date present but unparseable: keep the candidate, carry the raw
            # string, do NOT fabricate today as observed_at.
            evidence["date_raw"] = raw_listed
            observed = ""
        else:
            observed = listed or today.isoformat()
        out.append(
            SignalCandidate(
                signal_type="leadership_job_open",
                observed_at=observed,
                natural_key=f"lijob:{r.get('job_id') or title}",
                title=title,
                url=r.get("apply_url") or r.get("url"),
                confidence=0.85,
                evidence_data=evidence,
            )
        )
    return out
