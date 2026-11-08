"""Source adapter contract. plan() and parse() are pure."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from src.core.models import Account, Document


@dataclass(frozen=True)
class FetchTask:
    source: str
    url: str
    domain: str | None = None
    method: str = "GET"
    headers: dict = field(default_factory=dict)
    json_body: dict | None = None
    cursor_key: str | None = None
    meta: dict = field(default_factory=dict)
    # Pre-encoded form body (e.g. Google batchexecute "f.req=..."). Sent via
    # httpx `content=`; Content-Type lives on task.headers. Kept separate from
    # json_body because batchexecute rejects JSON bodies.
    data_body: str | None = None


@dataclass
class SignalCandidate:
    signal_type: str
    observed_at: str
    natural_key: str
    title: str | None = None
    summary: str | None = None
    url: str | None = None
    confidence: float = 0.8
    person_key: str | None = None
    evidence_data: dict = field(default_factory=dict)
    domain_override: str | None = None


class SourceAdapter(ABC):
    key: str
    tier: str = "http"
    cadence_hours: int = 24
    requires: tuple[str, ...] = ()
    emits: tuple[str, ...] = ()

    @abstractmethod
    def plan(self, account: Account, cursor: str | None) -> list[FetchTask]:
        """PURE. Build the request list. No I/O. Must be deterministic."""

    @abstractmethod
    def parse(self, doc: Document, account: Account, task_meta: dict) -> list[SignalCandidate]:
        """PURE. Bytes in, candidates out. No I/O, no clock reads except today via meta."""

    def next_cursor(self, doc: Document, candidates: list[SignalCandidate]) -> str | None:
        dates = [c.observed_at for c in candidates if c.observed_at]
        if not dates:
            return None
        return max(dates)

    def harvest_jobs(self, doc: Document, account: Account, task_meta: dict) -> list:
        return []

    def local_harvest(self, *, db, account, today, task_meta: dict) -> list:
        return []

    def follow_tasks(self, doc: Document, account: Account, task_meta: dict) -> list[FetchTask]:
        return []
