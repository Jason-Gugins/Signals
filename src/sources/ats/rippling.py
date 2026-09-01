"""PURE Rippling job-board parser + collector adapter.

Rippling ATS hosts public career boards at ``https://ats.rippling.com/{token}``.
The fixture here is hand-built from the board's public JSON shape
(``{"jobs": [ {id, title, url, department, location, is_remote,
employment_type, posted_at, description, ...} ]}``); the parser is written
conservatively (key aliases, both list and ``jobs``-wrapped shapes, string or
dict locations) so a live probe can tighten it later without contract changes.
Endpoint used by plan() is fixture-driven and should be re-verified live
before enabling in production wiring.
"""

from __future__ import annotations

import json
from typing import Optional

from src.core.models import Account, Document
from src.core.textutil import to_iso_date
from src.sources.base import FetchTask, SignalCandidate, SourceAdapter
from src.sources.ats.common import JobPost, parse_location, strip_html
from src.sources.registry import register


def _loc_str(loc) -> Optional[str]:
    if isinstance(loc, str):
        return loc or None
    if isinstance(loc, dict):
        return loc.get("name") or loc.get("label")
    return None


def parse_rippling(body: bytes) -> list[JobPost]:
    raw = json.loads(body)
    jobs = raw.get("jobs") if isinstance(raw, dict) else raw
    if not isinstance(jobs, list):
        jobs = []
    out: list[JobPost] = []
    for j in jobs:
        if not isinstance(j, dict):
            continue
        loc = _loc_str(j.get("location"))
        city, region, country, remote = parse_location(loc)
        out.append(
            JobPost(
                external_id=str(j.get("id") or j.get("job_id") or ""),
                title=j.get("title") or "",
                url=j.get("url") or j.get("job_url") or j.get("apply_url") or "",
                posted_at=to_iso_date(j.get("posted_at") or j.get("created_at") or j.get("published_at")),
                department=j.get("department") or j.get("team"),
                location_raw=loc,
                city=city,
                region=region,
                country=country,
                remote=(True if j.get("is_remote") else remote),
                employment_type=j.get("employment_type") or j.get("type"),
                description=strip_html(j.get("description")),
                comp_min=j.get("salary_min"),
                comp_max=j.get("salary_max"),
                comp_currency=j.get("salary_currency"),
            )
        )
    out.sort(key=lambda x: x.external_id)
    return out


@register
class RipplingSource(SourceAdapter):
    """Rippling job-board collector — board JSON at jobs.rippling.com/{token}."""

    key = "ats_rippling"
    tier = "http"
    cadence_hours = 12
    requires = ("ats_token",)

    def plan(self, account: Account, cursor: Optional[str]) -> list[FetchTask]:
        token = account.ats_token
        return [
            FetchTask(
                source=self.key,
                url=f"https://jobs.rippling.com/{token}/jobs",
                domain=account.domain,
                meta={"vendor": "rippling", "token": token},
            )
        ]

    def harvest_jobs(self, doc, account, task_meta):
        return parse_rippling(doc.body) if doc.body else []

    def parse(self, doc: Document, account: Account, task_meta: dict) -> list[SignalCandidate]:
        return []
