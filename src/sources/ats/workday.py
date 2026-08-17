"""Workday Candidate Experience Service job board parser.

Live recon 2026-08-16: POST nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs
→ 200 JSON {total, jobPostings[title,externalPath,locationsText,postedOn]}.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta

from src.sources.ats.common import JobPost, parse_location


def workday_endpoint(tenant: str, wd: str, site: str) -> str:
    return f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"


def workday_body(offset: int, limit: int = 20) -> dict:
    return {"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": ""}


def workday_offsets(total: int, limit: int = 20, cap: int = 200) -> list[int]:
    if total <= 0 or limit <= 0:
        return []
    n = min(total, cap)
    return list(range(0, n, limit))


_REL = re.compile(r"posted\s+(yesterday|today|(\d+)\+?\s+days?\s+ago)", re.I)


def _posted_on(text: str | None, today: date) -> tuple[str | None, bool]:
    if not text:
        return None, False
    m = _REL.search(text)
    if not m:
        return None, False
    if m.group(1).lower() == "today":
        return today.isoformat(), False
    if m.group(1).lower() == "yesterday":
        return (today - timedelta(days=1)).isoformat(), False
    n = int(m.group(2))
    approx = "+" in m.group(0)
    return (today - timedelta(days=n)).isoformat(), approx


def parse_workday(body: bytes, *, base: str, today: date) -> list[JobPost]:
    raw = json.loads(body)
    jobs = raw.get("jobPostings") or []
    out = []
    for i, j in enumerate(jobs):
        path = j.get("externalPath") or ""
        url = path if path.startswith("http") else (base.rstrip("/") + "/" + path.lstrip("/"))
        loc = j.get("locationsText")
        city, region, country, remote = parse_location(loc)
        posted, approx = _posted_on(j.get("postedOn"), today)
        extra = {"posted_approx": True} if approx else {}
        out.append(
            JobPost(
                external_id=path or str(i),
                title=j.get("title") or "",
                url=url,
                posted_at=posted,
                location_raw=loc,
                city=city,
                country=country,
                remote=remote,
                extra=extra,
            )
        )
    out.sort(key=lambda x: x.external_id)
    return out
