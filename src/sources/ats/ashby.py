"""PURE Ashby job-board parser."""

from __future__ import annotations

import json

from src.core.textutil import to_iso_date
from src.sources.ats.common import JobPost, parse_location


def parse_ashby(body: bytes) -> list[JobPost]:
    raw = json.loads(body)
    jobs = raw.get("jobs") or raw.get("jobPostings") or []
    out = []
    for j in jobs:
        loc = j.get("locationName") or j.get("location")
        city, region, country, remote = parse_location(loc if isinstance(loc, str) else None)
        comp = j.get("compensation") or {}
        out.append(
            JobPost(
                external_id=str(j.get("id") or j.get("jobId")),
                title=j.get("title") or "",
                url=j.get("jobUrl") or j.get("applyUrl") or "",
                posted_at=to_iso_date(j.get("publishedAt") or j.get("publishedDate")),
                department=j.get("departmentName") or j.get("department"),
                location_raw=loc if isinstance(loc, str) else None,
                city=city,
                region=region,
                country=country,
                remote=remote,
                comp_min=comp.get("minValue") or comp.get("min"),
                comp_max=comp.get("maxValue") or comp.get("max"),
                comp_currency=comp.get("currencyCode") or comp.get("currency"),
            )
        )
    out.sort(key=lambda x: x.external_id)
    return out
