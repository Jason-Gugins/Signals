"""Recruitee offers parser.

Working endpoint (recon 2026-08-16, public offers API):
https://{token}.recruitee.com/api/offers/
"""

from __future__ import annotations

import json

from src.core.textutil import to_iso_date
from src.sources.ats.common import JobPost, parse_location


def parse_recruitee(body: bytes) -> list[JobPost]:
    raw = json.loads(body)
    jobs = raw.get("offers") or []
    out = []
    for j in jobs:
        loc = j.get("location") or j.get("city")
        loc_s = loc if isinstance(loc, str) else None
        city, region, country, remote = parse_location(loc_s)
        dept = j.get("department")
        if isinstance(dept, dict):
            dept = dept.get("name")
        out.append(
            JobPost(
                external_id=str(j.get("id") or j.get("slug")),
                title=j.get("title") or "",
                url=j.get("careers_url") or j.get("url") or "",
                posted_at=to_iso_date(j.get("published_at") or j.get("created_at")),
                department=dept,
                location_raw=loc_s,
                city=city,
                country=country,
                remote=remote,
            )
        )
    out.sort(key=lambda x: x.external_id)
    return out
