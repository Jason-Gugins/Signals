"""PURE posts → launch / appearance / hiring-adjacent signals."""

from __future__ import annotations

import re
from datetime import date

from src.core.models import Contact
from src.core.textutil import to_iso_date
from src.sources.base import SignalCandidate

_LAUNCH = re.compile(r"\b(launch|introducing|announcing)\b", re.I)
_HIRE = re.compile(r"\b(we're hiring|we are hiring|join our team)\b", re.I)
_DEPT = re.compile(r"\b(sales|engineering|marketing|product)\b", re.I)


def posts_to_candidates(rows, domain, contacts, *, today: date) -> list[SignalCandidate]:
    by_slug = {c.linkedin_slug: c for c in contacts if c.linkedin_slug}
    out = []
    for r in rows:
        text = r.get("text") or ""
        observed = to_iso_date(r.get("posted_at")) or today.isoformat()
        urn = r.get("post_urn") or observed
        if r.get("author_kind") == "company" and _LAUNCH.search(text):
            out.append(
                SignalCandidate(
                    signal_type="product_launch",
                    observed_at=observed,
                    natural_key=f"lipost:{urn}",
                    title=text[:80],
                    url=r.get("permalink"),
                    confidence=0.6,
                    evidence_data={},
                )
            )
        if r.get("author_kind") == "person":
            slug = r.get("author_slug")
            out.append(
                SignalCandidate(
                    signal_type="content_appearance",
                    observed_at=observed,
                    natural_key=f"liperson:{urn}",
                    title=text[:80],
                    url=r.get("permalink"),
                    confidence=0.4,
                    person_key=slug,
                    evidence_data={},
                )
            )
        if _HIRE.search(text):
            dept = _DEPT.search(text)
            if dept:
                out.append(
                    SignalCandidate(
                        signal_type="department_expansion",
                        observed_at=observed,
                        natural_key=f"lihire:{urn}",
                        title=text[:80],
                        confidence=0.4,
                        evidence_data={"department": dept.group(1).title()},
                    )
                )
    return out
