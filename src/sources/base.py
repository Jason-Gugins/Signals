"""Source adapter contract. plan() and parse() are pure."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from src.core.models import Account, Document


@dataclass(frozen=True)
class FetchTask:
    source: str
    url: str
    domain: Optional[str] = None
    method: str = "GET"
    headers: dict = field(default_factory=dict)
    json_body: Optional[dict] = None
    cursor_key: Optional[str] = None
    meta: dict = field(default_factory=dict)


@dataclass
class SignalCandidate:
    signal_type: str
    observed_at: str
    natural_key: str
    title: Optional[str] = None
    summary: Optional[str] = None
    url: Optional[str] = None
    confidence: float = 0.8
    person_key: Optional[str] = None
    evidence_data: dict = field(default_factory=dict)
    domain_override: Optional[str] = None


class SourceAdapter(ABC):
    key: str
    tier: str = "http"
    cadence_hours: int = 24
    requires: tuple[str, ...] = ()
    emits: tuple[str, ...] = ()

    @abstractmethod
    def plan(self, account: Account, cursor: Optional[str]) -> list[FetchTask]:
        """PURE. Build the request list. No I/O. Must be deterministic."""

    @abstractmethod
    def parse(self, doc: Document, account: Account, task_meta: dict) -> list[SignalCandidate]:
        """PURE. Bytes in, candidates out. No I/O, no clock reads except today via meta."""

    def next_cursor(self, doc: Document, candidates: list[SignalCandidate]) -> Optional[str]:
        dates = [c.observed_at for c in candidates if c.observed_at]
        if not dates:
            return None
        return max(dates)
