"""ATS collectors (Greenhouse + Lever)."""

from __future__ import annotations

from typing import Optional

from src.core.models import Account, Document
from src.sources.ats.greenhouse import parse_greenhouse
from src.sources.ats.lever import parse_lever
from src.sources.base import FetchTask, SignalCandidate, SourceAdapter
from src.sources.registry import register


@register
class GreenhouseSource(SourceAdapter):
    key = "ats_greenhouse"
    tier = "http"
    cadence_hours = 12
    requires = ("ats_token",)
    emits = ()

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

    def parse(self, doc: Document, account: Account, task_meta: dict) -> list[SignalCandidate]:
        if not doc.body:
            return []
        parse_greenhouse(doc.body)
        return []


@register
class LeverSource(SourceAdapter):
    key = "ats_lever"
    tier = "http"
    cadence_hours = 12
    requires = ("ats_token",)
    emits = ()

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

    def parse(self, doc: Document, account: Account, task_meta: dict) -> list[SignalCandidate]:
        if not doc.body:
            return []
        parse_lever(doc.body)
        return []
