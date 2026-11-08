"""Dataclass models for accounts, contacts, signals, and documents.

`_json_dump`, `_json_load`, and `_row_to_dataclass` are ported from
../Linkedin/src/models.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from _typeshed import DataclassInstance

def _json_dump(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, dict)) and not value:
        return None
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    # Defensive: every current caller passes only list/dict (or None), so this
    # fall-through is dead code today; the cast labels the declared contract
    # without changing behavior for hypothetical scalar inputs.
    return cast("str | None", value)


def _json_load(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def _row_to_dataclass[T: DataclassInstance](cls: type[T], row: dict) -> T:
    data = dict(row)
    valid = {f.name for f in fields(cls)}
    data = {k: v for k, v in data.items() if k in valid}
    return cls(**data)


@dataclass
class Account:
    domain: str
    name: str | None = None
    legal_name: str | None = None
    linkedin_slug: str | None = None
    linkedin_company_id: str | None = None
    repvue_slug: str | None = None
    g2_slug: str | None = None
    app_store_id: str | None = None
    play_id: str | None = None
    subreddit: str | None = None
    cik: str | None = None
    ticker: str | None = None
    ats_vendor: str | None = None
    ats_token: str | None = None
    careers_url: str | None = None
    blog_feed_url: str | None = None
    industry: str | None = None
    sic_code: str | None = None
    employee_count: int | None = None
    employee_count_at: str | None = None
    hq_country: str | None = None
    hq_region: str | None = None
    hq_city: str | None = None
    founded: str | None = None
    company_type: str | None = None
    cohort: str | None = None
    seed_source: str | None = None
    icp_fit: float = 1.0
    icp_reasons: list = field(default_factory=list)
    disqualified: bool = False
    disqualify_reason: str | None = None
    score: float | None = None
    tier: int | None = None
    buying_window: str | None = None
    scored_at: str | None = None
    extra_data: dict = field(default_factory=dict)

    def to_db_row(self) -> dict:
        return {
            "domain": self.domain,
            "name": self.name,
            "legal_name": self.legal_name,
            "linkedin_slug": self.linkedin_slug,
            "linkedin_company_id": self.linkedin_company_id,
            "repvue_slug": self.repvue_slug,
            "g2_slug": self.g2_slug,
            "app_store_id": self.app_store_id,
            "play_id": self.play_id,
            "subreddit": self.subreddit,
            "cik": self.cik,
            "ticker": self.ticker,
            "ats_vendor": self.ats_vendor,
            "ats_token": self.ats_token,
            "careers_url": self.careers_url,
            "blog_feed_url": self.blog_feed_url,
            "industry": self.industry,
            "sic_code": self.sic_code,
            "employee_count": self.employee_count,
            "employee_count_at": self.employee_count_at,
            "hq_country": self.hq_country,
            "hq_region": self.hq_region,
            "hq_city": self.hq_city,
            "founded": self.founded,
            "company_type": self.company_type,
            "cohort": self.cohort,
            "seed_source": self.seed_source,
            "icp_fit": self.icp_fit,
            "icp_reasons": _json_dump(self.icp_reasons),
            "disqualified": int(self.disqualified),
            "disqualify_reason": self.disqualify_reason,
            "score": self.score,
            "tier": self.tier,
            "buying_window": self.buying_window,
            "scored_at": self.scored_at,
            "extra_data": _json_dump(self.extra_data),
        }

    @classmethod
    def from_db_row(cls, row: dict) -> Account:
        data = dict(row)
        data["icp_reasons"] = _json_load(data.get("icp_reasons"), [])
        data["extra_data"] = _json_load(data.get("extra_data"), {})
        data["disqualified"] = bool(data.get("disqualified", 0))
        if data.get("icp_fit") is None:
            data["icp_fit"] = 1.0
        return _row_to_dataclass(cls, data)


@dataclass
class Contact:
    person_key: str
    domain: str | None = None
    name: str | None = None
    title: str | None = None
    seniority: str | None = None
    persona: str | None = None
    department: str | None = None
    linkedin_slug: str | None = None
    linkedin_url: str | None = None
    location: str | None = None
    is_champion: bool = False
    role_started_at: str | None = None
    prior_domain: str | None = None
    email_guess: str | None = None
    email_pattern: str | None = None
    extra_data: dict = field(default_factory=dict)

    def to_db_row(self) -> dict:
        return {
            "person_key": self.person_key,
            "domain": self.domain,
            "name": self.name,
            "title": self.title,
            "seniority": self.seniority,
            "persona": self.persona,
            "department": self.department,
            "linkedin_slug": self.linkedin_slug,
            "linkedin_url": self.linkedin_url,
            "location": self.location,
            "is_champion": int(self.is_champion),
            "role_started_at": self.role_started_at,
            "prior_domain": self.prior_domain,
            "email_guess": self.email_guess,
            "email_pattern": self.email_pattern,
            "extra_data": _json_dump(self.extra_data),
        }

    @classmethod
    def from_db_row(cls, row: dict) -> Contact:
        data = dict(row)
        data["extra_data"] = _json_load(data.get("extra_data"), {})
        data["is_champion"] = bool(data.get("is_champion", 0))
        return _row_to_dataclass(cls, data)


@dataclass
class Signal:
    signal_id: str
    domain: str
    signal_type: str
    category: str
    origin: str
    catalyst: str
    polarity: str
    observed_at: str
    source: str
    degree: int = 0
    person_key: str | None = None
    title: str | None = None
    summary: str | None = None
    evidence: str | None = None
    evidence_data: dict = field(default_factory=dict)
    url: str | None = None
    confidence: float = 0.8
    first_seen_at: str | None = None
    last_seen_at: str | None = None
    raw_ref: str | None = None
    superseded_by: str | None = None

    def to_db_row(self) -> dict:
        return {
            "signal_id": self.signal_id,
            "domain": self.domain,
            "person_key": self.person_key,
            "signal_type": self.signal_type,
            "category": self.category,
            "degree": self.degree,
            "origin": self.origin,
            "catalyst": self.catalyst,
            "polarity": self.polarity,
            "title": self.title,
            "summary": self.summary,
            "evidence": self.evidence,
            "evidence_data": _json_dump(self.evidence_data),
            "url": self.url,
            "source": self.source,
            "confidence": self.confidence,
            "observed_at": self.observed_at,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "raw_ref": self.raw_ref,
            "superseded_by": self.superseded_by,
        }

    @classmethod
    def from_db_row(cls, row: dict) -> Signal:
        data = dict(row)
        data["evidence_data"] = _json_load(data.get("evidence_data"), {})
        return _row_to_dataclass(cls, data)


@dataclass
class Document:
    doc_id: str
    source: str
    url: str | None = None
    domain: str | None = None
    method: str = "GET"
    content_type: str | None = None
    status: int | None = None
    byte_size: int | None = None
    path: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    fetched_at: str | None = None
    parsed_at: str | None = None
    parse_error: str | None = None
    body: bytes | None = None  # NOT persisted; lives in rawstore

    def to_db_row(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "source": self.source,
            "domain": self.domain,
            "url": self.url,
            "method": self.method,
            "content_type": self.content_type,
            "status": self.status,
            "byte_size": self.byte_size,
            "path": self.path,
            "etag": self.etag,
            "last_modified": self.last_modified,
            "fetched_at": self.fetched_at,
            "parsed_at": self.parsed_at,
            "parse_error": self.parse_error,
        }

    @classmethod
    def from_db_row(cls, row: dict) -> Document:
        return _row_to_dataclass(cls, dict(row))


@dataclass
class ScoreComponents:
    per_signal: list = field(default_factory=list)
    combos: list = field(default_factory=list)
    raw: float = 0.0
    score: float = 0.0

    def to_db_row(self) -> dict:
        return {
            "per_signal": _json_dump(self.per_signal),
            "combos": _json_dump(self.combos),
            "raw": self.raw,
            "score": self.score,
        }

    @classmethod
    def from_db_row(cls, row: dict) -> ScoreComponents:
        data = dict(row)
        data["per_signal"] = _json_load(data.get("per_signal"), [])
        data["combos"] = _json_load(data.get("combos"), [])
        return _row_to_dataclass(cls, data)
