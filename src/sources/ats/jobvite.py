"""PURE Jobvite job-board parser + collector adapter.

Jobvite public career boards live at ``https://jobs.jobvite.com/{token}`` and
expose a JSON feed of open positions shaped like
``{"jobs": [ {id, title, url, category, location, remote, type,
publishedDate, description, ...} ]}`` (hand-built fixture; no live probe
used). The parser is conservative: accepts both list and ``jobs``-wrapped
shapes, string or dict locations, and multiple date-key aliases.
"""

from __future__ import annotations

import json
from typing import Optional

from src.core.textutil import to_iso_date
from src.sources.ats.common import JobPost, parse_location, strip_html


def _loc_str(loc) -> Optional[str]:
    if isinstance(loc, str):
        return loc or None
    if isinstance(loc, dict):
        return loc.get("name") or loc.get("label")
    return None


def parse_jobvite(body: bytes) -> list[JobPost]:
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
                external_id=str(j.get("id") or j.get("jobId") or ""),
                title=j.get("title") or j.get("name") or "",
                url=j.get("url") or j.get("jobUrl") or j.get("applyUrl") or "",
                posted_at=to_iso_date(
                    j.get("publishedDate") or j.get("publishedAt") or j.get("createdDate")
                ),
                department=j.get("category") or j.get("department") or j.get("team"),
                location_raw=loc,
                city=city,
                region=region,
                country=country,
                remote=(True if j.get("remote") else remote),
                employment_type=j.get("type") or j.get("jobType"),
                description=strip_html(j.get("description") or j.get("descriptionPlain")),
            )
        )
    out.sort(key=lambda x: x.external_id)
    return out


from src.core.models import Account, Document
from src.sources.base import FetchTask, SignalCandidate, SourceAdapter
from src.sources.registry import register


@register
class JobviteSource(SourceAdapter):
    """Jobvite job-board collector — public JSON feed at jobs.jobvite.com/{token}."""

    key = "ats_jobvite"
    tier = "http"
    cadence_hours = 12
    requires = ("ats_token",)

    def plan(self, account: Account, cursor: Optional[str]) -> list[FetchTask]:
        token = account.ats_token
        return [
            FetchTask(
                source=self.key,
                url=f"https://jobs.jobvite.com/{token}/jobs",
                domain=account.domain,
                meta={"vendor": "jobvite", "token": token},
            )
        ]

    def harvest_jobs(self, doc, account, task_meta):
        return parse_jobvite(doc.body) if doc.body else []

    def parse(self, doc: Document, account: Account, task_meta: dict) -> list[SignalCandidate]:
        return []
