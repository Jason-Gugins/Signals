"""PURE SmartRecruiters postings parser + pagination offsets."""

from __future__ import annotations

import json

from src.core.textutil import to_iso_date
from src.sources.ats.common import JobPost, parse_location


def smartrecruiters_pages(total: int, limit: int) -> list[int]:
    if total <= 0 or limit <= 0:
        return []
    return list(range(0, total, limit))


def parse_smartrecruiters(body: bytes) -> list[JobPost]:
    raw = json.loads(body)
    jobs = raw.get("content") or raw.get("jobs") or []
    out = []
    for j in jobs:
        loc = j.get("location") or {}
        raw_loc = ", ".join(x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x)
        city, region, country, remote = parse_location(raw_loc or loc.get("city"))
        dept = (j.get("department") or {}).get("label") if isinstance(j.get("department"), dict) else j.get("department")
        comp = j.get("compensation") or {}
        out.append(
            JobPost(
                external_id=str(j.get("id") or j.get("ref")),
                title=j.get("name") or j.get("title") or "",
                url=j.get("ref") or "",
                posted_at=to_iso_date(j.get("releasedDate") or j.get("releasedTimestamp")),
                department=dept,
                location_raw=raw_loc or None,
                city=city or loc.get("city"),
                region=region or loc.get("region"),
                country=country or loc.get("country"),
                remote=remote,
                comp_min=comp.get("min"),
                comp_max=comp.get("max"),
                comp_currency=comp.get("currency"),
            )
        )
    out.sort(key=lambda x: x.external_id)
    return out
