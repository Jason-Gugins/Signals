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
    # --- generic runner opt-ins (see CollectorRunner._run_pair) -------------
    # Details are emitted per LIST page: page 0's land in pass 1, and pages
    # 1..N's only exist once their pagination POSTs have run (pass 1) -- so
    # their detail tasks need a THIRD wave to execute. With the default 2-wave
    # budget they were emitted and then dropped, which is why a live 83-posting
    # board only ever described the 10 postings on page 0.
    follow_passes = 3
    # Ask the runner to hand this adapter the set of postings that already
    # carry a description (and to enforce the per-cycle detail budget below).
    detail_selection = True

    @property
    def detail_budget(self) -> int:
        """Detail GETs allowed per account per CYCLE (config-driven).

        Read at CALL time, never at import time, so a config edit (or a test's
        loader monkeypatch) takes effect without a reload. 0 = no details.
        """
        try:
            return int((load_ats_workday_cfg() or {}).get("detail_follow_max", 10) or 0)
        except (TypeError, ValueError):
            return 10

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
        # descriptions live. Emitted per page, so the cost is pages x the
        # runner's per-cycle budget; every detail task carries no jobPostings,
        # so it yields no further follow tasks.
        #
        # ROTATION (F8): the adapter no longer caps its own output -- a
        # positional `postings[:cap]` slice meant the same first-N postings won
        # every cycle and the rest could never be reached. Instead it emits
        # every UNDESCRIBED posting it can see, in page order, and skips the
        # ones the runner says already have a description; the runner enforces
        # the per-cycle total (detail_budget) and the extra pass (follow_passes).
        described = {str(x) for x in (meta.get("described_external_ids") or ())}
        for j in postings:
            posting = j or {}
            path = posting.get("externalPath") or ""
            if not path or path in described:
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
                        "title": posting.get("title") or None,
                    },
                )
            )
        return out

    def parse(self, doc, account, task_meta):
        return []
