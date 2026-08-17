"""ATS collectors."""

from __future__ import annotations

from datetime import date
from typing import Optional

from src.core.models import Account, Document
from src.sources.ats.ashby import parse_ashby
from src.sources.ats.greenhouse import parse_greenhouse
from src.sources.ats.lever import parse_lever
from src.sources.ats.recruitee import parse_recruitee
from src.sources.ats.smartrecruiters import parse_smartrecruiters
from src.sources.ats.workable import parse_workable
from src.sources.ats.workday import parse_workday, workday_body, workday_endpoint
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
        return parse_workday(
            doc.body,
            base=task_meta.get("base") or "",
            today=_today(task_meta),
        )

    def parse(self, doc, account, task_meta):
        return []
