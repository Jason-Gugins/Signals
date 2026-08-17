"""Workable public board parser.

Working endpoint (recon 2026-08-16, live 200 on apply.workable.com/api/v1/widget/accounts/coldquanta?details=true):
https://apply.workable.com/api/v1/widget/accounts/{token}?details=true
SPI v3 /spi/v3/jobs returned 401 without auth — widget is the public path.
"""

from __future__ import annotations

import json

from src.core.textutil import to_iso_date
from src.sources.ats.common import JobPost, parse_location


def parse_workable(body: bytes) -> list[JobPost]:
    raw = json.loads(body)
    jobs = raw.get("jobs") or []
    out = []
    for j in jobs:
        loc = j.get("location")
        loc_s = loc.get("city") if isinstance(loc, dict) else loc
        city, region, country, remote = parse_location(loc_s if isinstance(loc_s, str) else None)
        out.append(
            JobPost(
                external_id=str(j.get("shortcode") or j.get("id")),
                title=j.get("title") or "",
                url=j.get("url") or j.get("application_url") or "",
                posted_at=to_iso_date(j.get("created_at") or j.get("published_on")),
                department=j.get("department"),
                location_raw=loc_s if isinstance(loc_s, str) else None,
                city=city,
                country=country,
                remote=remote or (j.get("remote") is True),
            )
        )
    out.sort(key=lambda x: x.external_id)
    return out
