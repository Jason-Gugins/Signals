"""PURE Lever postings parser. createdAt is epoch milliseconds."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from src.sources.ats.common import JobPost, parse_location


def parse_lever(body: bytes) -> list[JobPost]:
    raw = json.loads(body)
    jobs = raw if isinstance(raw, list) else raw.get("data") or raw.get("postings") or []
    out: list[JobPost] = []
    for j in jobs:
        cats = j.get("categories") or {}
        loc = cats.get("location")
        city, region, country, remote = parse_location(loc)
        if (j.get("workplaceType") or "").lower() == "remote":
            remote = True
        posted = None
        created = j.get("createdAt")
        if isinstance(created, (int, float)):
            posted = datetime.fromtimestamp(created / 1000.0, tz=timezone.utc).date().isoformat()
        salary = j.get("salaryRange") or {}
        out.append(
            JobPost(
                external_id=str(j.get("id")),
                title=j.get("text") or j.get("title") or "",
                url=j.get("hostedUrl") or j.get("applyUrl") or "",
                posted_at=posted,
                department=cats.get("department"),
                location_raw=loc,
                city=city,
                region=region,
                country=country,
                remote=remote,
                employment_type=cats.get("commitment"),
                description=j.get("descriptionPlain") or j.get("description"),
                comp_min=salary.get("min"),
                comp_max=salary.get("max"),
                comp_currency=salary.get("currency"),
            )
        )
    out.sort(key=lambda x: x.external_id)
    return out
