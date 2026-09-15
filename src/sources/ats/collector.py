"""ATS collectors."""

from __future__ import annotations

from datetime import date
from typing import Optional

from src.core.models import Account, Document
from src.sources.ats.ashby import parse_ashby
from src.sources.ats.common import JobPost
from src.sources.ats.greenhouse import parse_greenhouse
from src.sources.ats.lever import parse_lever
from src.sources.ats.recruitee import parse_recruitee
from src.sources.ats.smartrecruiters import parse_smartrecruiters, smartrecruiters_pages
from src.sources.ats.thresholds import load_ats_workday_cfg
from src.sources.ats.workable import parse_workable
from src.sources.ats.workday import (
    parse_workday,
    parse_workday_detail,
    workday_body,
    workday_endpoint,
    workday_offsets,
)
from src.sources.base import FetchTask, SignalCandidate, SourceAdapter
from src.sources.registry import register


def _today(meta: dict) -> date:
    return date.fromisoformat(meta.get("today") or "2026-08-16")


@register
class GreenhouseSource(SourceAdapter):
    key = "ats_greenhouse"
    tier = "http"
    cadence_hours = 12
    requires = ("ats_token",)

    def plan(self, account: Account, cursor: Optional[str]) -> list[FetchTask]:
        token = account.ats_token
        return [
            FetchTask(
                source=self.key,
                url=f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true",
                domain=account.domain,
                meta={"vendor": "greenhouse", "token": token},
            )
        ]

    def harvest_jobs(self, doc, account, task_meta):
        return parse_greenhouse(doc.body) if doc.body else []

    def parse(self, doc: Document, account: Account, task_meta: dict) -> list[SignalCandidate]:
        return []


@register
class LeverSource(SourceAdapter):
    key = "ats_lever"
    tier = "http"
    cadence_hours = 12
    requires = ("ats_token",)

    def plan(self, account: Account, cursor: Optional[str]) -> list[FetchTask]:
        token = account.ats_token
        return [
            FetchTask(
                source=self.key,
                url=f"https://api.lever.co/v0/postings/{token}?mode=json",
                domain=account.domain,
                meta={"vendor": "lever", "token": token},
            )
        ]

    def harvest_jobs(self, doc, account, task_meta):
        return parse_lever(doc.body) if doc.body else []

    def parse(self, doc: Document, account: Account, task_meta: dict) -> list[SignalCandidate]:
        return []


@register
class AshbySource(SourceAdapter):
    key = "ats_ashby"
    tier = "http"
    cadence_hours = 12
    requires = ("ats_token",)

    def plan(self, account: Account, cursor: Optional[str]) -> list[FetchTask]:
        token = account.ats_token
        return [
            FetchTask(
                source=self.key,
                url=f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true",
                domain=account.domain,
                meta={"token": token},
            )
        ]

    def harvest_jobs(self, doc, account, task_meta):
        return parse_ashby(doc.body) if doc.body else []

    def parse(self, doc, account, task_meta):
        return []


@register
class SmartRecruitersSource(SourceAdapter):
    key = "ats_smartrecruiters"
    tier = "http"
    cadence_hours = 24
    requires = ("ats_token",)

    def plan(self, account: Account, cursor: Optional[str]) -> list[FetchTask]:
        token = account.ats_token
        return [
            FetchTask(
                source=self.key,
                url=f"https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100&offset=0",
                domain=account.domain,
                meta={"token": token},
            )
        ]

    def harvest_jobs(self, doc, account, task_meta):
        return parse_smartrecruiters(doc.body) if doc.body else []

    def follow_tasks(self, doc, account, task_meta):
        if not doc.body or (task_meta or {}).get("offset"):
            return []
        try:
            raw = __import__("json").loads(doc.body)
            total = int(raw.get("totalFound") or 0)
        except (TypeError, ValueError):
            return []
        token = account.ats_token
        out = []
        for off in smartrecruiters_pages(total, 100):
            if off == 0:
                continue
            out.append(
                FetchTask(
                    source=self.key,
                    url=f"https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100&offset={off}",
                    domain=account.domain,
                    meta={"token": token, "offset": off},
                )
            )
        return out

    def parse(self, doc, account, task_meta):
        return []


@register
class WorkableSource(SourceAdapter):
    key = "ats_workable"
    tier = "http"
    cadence_hours = 24
    requires = ("ats_token",)

    def plan(self, account: Account, cursor: Optional[str]) -> list[FetchTask]:
        token = account.ats_token
        return [
            FetchTask(
                source=self.key,
                url=f"https://apply.workable.com/api/v1/widget/accounts/{token}?details=true",
                domain=account.domain,
                meta={"token": token},
            )
        ]

    def harvest_jobs(self, doc, account, task_meta):
        return parse_workable(doc.body) if doc.body else []

    def parse(self, doc, account, task_meta):
        return []


@register
class RecruiteeSource(SourceAdapter):
    key = "ats_recruitee"
    tier = "http"
    cadence_hours = 24
    requires = ("ats_token",)

    def plan(self, account: Account, cursor: Optional[str]) -> list[FetchTask]:
        token = account.ats_token
        return [
            FetchTask(
                source=self.key,
                url=f"https://{token}.recruitee.com/api/offers/",
                domain=account.domain,
                meta={"token": token},
            )
        ]

    def harvest_jobs(self, doc, account, task_meta):
        return parse_recruitee(doc.body) if doc.body else []

    def parse(self, doc, account, task_meta):
        return []


@register
class WorkdaySource(SourceAdapter):
    key = "ats_workday"
    tier = "http"
    cadence_hours = 24
    requires = ("ats_token",)

    def plan(self, account: Account, cursor: Optional[str]) -> list[FetchTask]:
        token = account.ats_token or ""
        parts = token.split("/")
        tenant = parts[0] if parts else token
        wd = parts[1] if len(parts) > 1 else "wd5"
        site = parts[2] if len(parts) > 2 else tenant
        url = workday_endpoint(tenant, wd, site)
        return [
            FetchTask(
                source=self.key,
                url=url,
                domain=account.domain,
                method="POST",
                json_body=workday_body(0),
                meta={"token": token, "base": url.rsplit("/jobs", 1)[0]},
            )
        ]

    def harvest_jobs(self, doc, account, task_meta):
        if not doc.body:
            return []
        meta = task_meta or {}
        if meta.get("detail"):
            info = parse_workday_detail(doc.body)
            if not info:
                return []
            return [
                JobPost(
                    external_id=meta.get("external_id") or "",
                    # None, never "": COALESCE treats "" as present and would
                    # blank the title stored by the list pass.
                    title=meta.get("title") or None,
                    url=doc.url,
                    # None so the list pass's posted_at survives COALESCE (the
                    # detail payload carries only human text / a bool).
                    posted_at=None,
                    department=info.get("department"),
                    description=info.get("description"),
                    employment_type=info.get("employment_type"),
                )
            ]
        return parse_workday(
            doc.body,
            base=meta.get("base") or "",
            today=_today(task_meta),
        )

    def follow_tasks(self, doc, account, task_meta):
        if not doc.body:
            return []
        meta = task_meta or {}
        if meta.get("detail"):
            return []  # a detail payload has no jobPostings: never re-fetch
        try:
            page = __import__("json").loads(doc.body)
            total = int(page.get("total") or 0)
            postings = page.get("jobPostings") or []
        except (TypeError, ValueError):
            return []
        token = account.ats_token or ""
        parts = token.split("/")
        tenant = parts[0] if parts else token
        wd = parts[1] if len(parts) > 1 else "wd5"
        site = parts[2] if len(parts) > 2 else tenant
        url = workday_endpoint(tenant, wd, site)
        base = meta.get("base") or url.rsplit("/jobs", 1)[0]
        out = []
        # Pagination FIRST, so it is never starved by the detail cap.
        if not meta.get("offset"):
            for off in workday_offsets(total):
                if off == 0:
                    continue
                out.append(
                    FetchTask(
                        source=self.key,
                        url=url,
                        domain=account.domain,
                        method="POST",
                        json_body=workday_body(off),
                        meta={"token": token, "base": base, "offset": off},
                    )
                )
        # Details: the CXS job URL (base + externalPath) is the ONLY place
        # descriptions live. Emitted per page, so the cost is cap x pages; every
        # detail task carries no jobPostings, so it yields no further follow tasks.
        cap = int((load_ats_workday_cfg() or {}).get("detail_follow_max", 10) or 0)
        for j in postings[:cap] if cap > 0 else []:
            path = (j or {}).get("externalPath") or ""
            if not path:
                continue
            out.append(
                FetchTask(
                    source=self.key,
                    url=base.rstrip("/") + "/" + path.lstrip("/"),
                    domain=account.domain,
                    method="GET",
                    headers={"Accept": "application/json"},
                    meta={
                        "token": token,
                        "base": base,
                        "detail": True,
                        "external_id": path,
                        "title": j.get("title") or None,
                    },
                )
            )
        return out

    def parse(self, doc, account, task_meta):
        return []
