"""PURE Breezy HR job-board parser + collector adapter.

Working endpoint (live probe 2026-08-31, HTTP 200 on euler.breezy.hr/json):
``https://{token}.breezy.hr/json`` returns a top-level JSON **list** of
positions with: ``id`` (hex slug), ``friendly_id`` (slug with title),
``name`` (title), ``url`` (apply page), ``published_date`` (ISO-8601 with
millis), ``type`` (``{"id": "fullTime", "name": "Full-Time"}``),
``location`` (``{"name", "is_remote", "country": {"name"}, ...}``),
``locations`` (list of the same), ``department``, ``salary`` (string), and
``description`` (HTML, present on some boards). Probe evidence is saved
under ``data/probe/`` (gitignored).
"""

from __future__ import annotations

import json
from typing import Optional

from src.core.textutil import to_iso_date
from src.sources.ats.common import JobPost, parse_location, strip_html


def _loc_parts(j: dict) -> tuple[Optional[str], Optional[bool]]:
    loc = j.get("location")
    if not isinstance(loc, dict):
        loc = next((l for l in j.get("locations") or [] if isinstance(l, dict)), None)
    if not isinstance(loc, dict):
        return None, None
    name = loc.get("name")
    if not name:
        country = loc.get("country")
        name = country.get("name") if isinstance(country, dict) else None
    return (name or None), (True if loc.get("is_remote") else None)


def parse_breezy(body: bytes) -> list[JobPost]:
    raw = json.loads(body)
    jobs = raw if isinstance(raw, list) else raw.get("positions") or raw.get("jobs") or []
    out: list[JobPost] = []
    for j in jobs:
        if not isinstance(j, dict):
            continue
        loc, is_remote = _loc_parts(j)
        city, region, country, remote = parse_location(loc)
        jtype = j.get("type")
        if isinstance(jtype, dict):
            jtype = jtype.get("name")
        url = j.get("url") or j.get("apply_url") or ""
        out.append(
            JobPost(
                external_id=str(j.get("id") or j.get("friendly_id") or ""),
                title=j.get("name") or j.get("title") or "",
                url=url,
                posted_at=to_iso_date(j.get("published_date") or j.get("published_date_iso")),
                department=j.get("department"),
                location_raw=loc,
                city=city,
                region=region,
                country=country,
                remote=(True if is_remote else remote),
                employment_type=jtype,
                description=strip_html(j.get("description")),
            )
        )
    out.sort(key=lambda x: x.external_id)
    return out


from src.core.models import Account, Document
from src.sources.base import FetchTask, SignalCandidate, SourceAdapter
from src.sources.registry import register


@register
class BreezySource(SourceAdapter):
    """Breezy HR collector — public JSON list at {token}.breezy.hr/json (live-probed shape)."""

    key = "ats_breezy"
    tier = "http"
    cadence_hours = 12
    requires = ("ats_token",)

    def plan(self, account: Account, cursor: Optional[str]) -> list[FetchTask]:
        token = account.ats_token
        return [
            FetchTask(
                source=self.key,
                url=f"https://{token}.breezy.hr/json",
                domain=account.domain,
                meta={"vendor": "breezy", "token": token},
            )
        ]

    def harvest_jobs(self, doc, account, task_meta):
        return parse_breezy(doc.body) if doc.body else []

    def parse(self, doc: Document, account: Account, task_meta: dict) -> list[SignalCandidate]:
        return []
