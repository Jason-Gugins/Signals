"""PURE Greenhouse board parser."""

from __future__ import annotations

import json
from html import unescape

from src.core.textutil import to_iso_date
from src.sources.ats.common import JobPost, parse_location, strip_html


def parse_greenhouse(body: bytes) -> list[JobPost]:
    raw = json.loads(body)
    jobs = raw.get("jobs", raw if isinstance(raw, list) else [])
    out: list[JobPost] = []
    for j in jobs:
        loc = ((j.get("location") or {}).get("name")) if isinstance(j.get("location"), dict) else j.get("location")
        city, region, country, remote = parse_location(loc)
        depts = j.get("departments") or []
        dept = depts[0].get("name") if depts and isinstance(depts[0], dict) else None
        content = j.get("content")
        if content:
            content = strip_html(unescape(content))
        comp_min = comp_max = None
        currency = None
        ranges = j.get("pay_input_ranges") or []
        if ranges:
            r0 = ranges[0]
            if r0.get("min_cents") is not None:
                comp_min = r0["min_cents"] / 100.0
            if r0.get("max_cents") is not None:
                comp_max = r0["max_cents"] / 100.0
            currency = r0.get("currency_type")
        out.append(
            JobPost(
                external_id=str(j.get("id")),
                title=j.get("title") or "",
                url=j.get("absolute_url") or "",
                posted_at=to_iso_date(j.get("updated_at") or j.get("first_published")),
                department=dept,
                location_raw=loc,
                city=city,
                region=region,
                country=country,
                remote=remote,
                description=content,
                comp_min=comp_min,
                comp_max=comp_max,
                comp_currency=currency,
            )
        )
    out.sort(key=lambda x: x.external_id)
    return out
