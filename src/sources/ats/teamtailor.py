"""PURE Teamtailor job-board parser + collector adapter.

Teamtailor public career boards live at ``https://{token}.teamtailor.com``.
Two shapes are parsed:
- LIVE ``/jobs.json`` (JSON Feed 1.1, probed on recruitgo.teamtailor.com
  2026-10-04): a ``{"version", "title", "items": [...]}`` document where
  each item carries ``id``, ``title``, ``url``, ``date_published``
  (ISO-8601), ``content_html``/``summary``, and (best-effort)
  ``tags``/``location``. The collector fetches the SAME URL the Stage-6
  verifier probes, so this parser and ``_board_payload_ok`` must agree on
  the shape.
- the board HTML's embedded JSON blob (``#component-listing-json`` /
  ``window.__INITIAL_STATE__``-style) whose ``jobs`` entries carry
  ``id``, ``title``, ``hosted_url`` (or ``url``), ``created_at`` /
  ``published_at`` (ISO-8601), ``department`` (``{"name": ...}`` or string),
  ``location`` (``{"name"/"city"/"region"/"country", "remote": bool}``), and
  ``description`` — hand-built fixture. Conservative aliases throughout.
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
        parts = [loc.get(k) for k in ("name", "city", "region", "country")]
        joined = ", ".join(str(p) for p in parts if p)
        return joined or None
    return None


def _dept(d) -> Optional[str]:
    if isinstance(d, dict):
        return d.get("name")
    return d if isinstance(d, str) and d else None


def _dept_from_tags(tags) -> Optional[str]:
    """Best-effort department for JSON Feed items, which carry ``tags``
    (JSON Feed 1.1's list-of-strings labels field) and no department key:
    the first non-empty tag wins."""
    if isinstance(tags, list):
        return next((t for t in tags if isinstance(t, str) and t.strip()), None)
    return None


def parse_teamtailor(body: bytes) -> list[JobPost]:
    raw = json.loads(body)
    if isinstance(raw, list):
        jobs = raw
    elif isinstance(raw, dict):
        # JSON Feed 1.1 — the LIVE /jobs.json shape (probed on
        # recruitgo.teamtailor.com 2026-10-04): the job list sits under
        # "items" with title/url/date_published/content_html directly on
        # each item. This branch must stay in sync with _board_payload_ok
        # (ats_discovery), which accepts "items" or "jobs": the collector
        # fetches the SAME URL the verifier probes, so a payload the
        # verifier accepts but this parser cannot read stamps the account
        # with a collector that collects nothing.
        items = raw.get("items")
        if isinstance(items, list) and items:
            jobs = items
        else:
            # Accept: bare list under jobs; {"data": [jobs]} (JSON:API style);
            # {"data": {"jobs": [...]}} (embedded initial-state style); plain
            # {"jobs": [...]}.
            data = raw.get("data")
            if isinstance(data, dict) and isinstance(data.get("jobs"), list):
                jobs = data["jobs"]
            elif isinstance(raw.get("jobs"), list):
                jobs = raw["jobs"]
            elif isinstance(data, list):
                jobs = data
            else:
                jobs = []
    else:
        jobs = []
    if not isinstance(jobs, list):
        jobs = []
    out: list[JobPost] = []
    for j in jobs:
        if not isinstance(j, dict):
            continue
        attrs = j.get("attributes") if isinstance(j.get("attributes"), dict) else j
        loc = _loc_str(attrs.get("location") or attrs.get("region"))
        city, region, country, remote = parse_location(loc)
        # dict location with explicit remote flag (or top-level attrs flag)
        loc_raw = attrs.get("location")
        if isinstance(loc_raw, dict) and loc_raw.get("remote") is True:
            remote = True
        if attrs.get("remote") is True or attrs.get("is_remote") is True:
            remote = True
        out.append(
            JobPost(
                external_id=str(j.get("id") or attrs.get("id") or attrs.get("slug") or ""),
                title=attrs.get("title") or "",
                url=attrs.get("hosted_url") or attrs.get("url") or attrs.get("careers_url") or "",
                posted_at=to_iso_date(
                    attrs.get("created_at")
                    or attrs.get("published_at")
                    or attrs.get("date_published")  # JSON Feed items
                    or attrs.get("created")
                ),
                department=(
                    _dept(attrs.get("department"))
                    or _dept_from_tags(attrs.get("tags"))  # JSON Feed items
                ),
                location_raw=loc,
                city=city,
                region=region,
                country=country,
                remote=remote,
                employment_type=attrs.get("employment_type") or attrs.get("contract_type"),
                description=strip_html(
                    attrs.get("description")
                    or attrs.get("content_html")  # JSON Feed items
                    or attrs.get("summary")
                ),
            )
        )
    out.sort(key=lambda x: x.external_id)
    return out


from src.core.models import Account, Document
from src.sources.base import FetchTask, SignalCandidate, SourceAdapter
from src.sources.registry import register


@register
class TeamtailorSource(SourceAdapter):
    """Teamtailor collector — embedded-JSON career board at {token}.teamtailor.com."""

    key = "ats_teamtailor"
    tier = "http"
    cadence_hours = 12
    requires = ("ats_token",)

    def plan(self, account: Account, cursor: Optional[str]) -> list[FetchTask]:
        token = account.ats_token
        return [
            FetchTask(
                source=self.key,
                url=f"https://{token}.teamtailor.com/jobs.json",
                domain=account.domain,
                meta={"vendor": "teamtailor", "token": token},
            )
        ]

    def harvest_jobs(self, doc, account, task_meta):
        return parse_teamtailor(doc.body) if doc.body else []

    def parse(self, doc: Document, account: Account, task_meta: dict) -> list[SignalCandidate]:
        return []
