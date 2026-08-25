"""Dataclass models for accounts, contacts, signals, and documents.

`_json_dump`, `_json_load`, and `_row_to_dataclass` are ported from
../Linkedin/src/models.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from typing import Any, Optional


def _json_dump(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (list, dict)) and not value:
        return None
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    return value


def _json_load(value: Any, default):
    if value is None or value == "":
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def _row_to_dataclass(cls, row: dict):
    data = dict(row)
    valid = {f.name for f in fields(cls)}
    data = {k: v for k, v in data.items() if k in valid}
    return cls(**data)


@dataclass
class Account:
    domain: str
    name: Optional[str] = None
    legal_name: Optional[str] = None
    linkedin_slug: Optional[str] = None
    linkedin_company_id: Optional[str] = None
    repvue_slug: Optional[str] = None
    g2_slug: Optional[str] = None
    cik: Optional[str] = None
    ticker: Optional[str] = None
    ats_vendor: Optional[str] = None
    ats_token: Optional[str] = None
    careers_url: Optional[str] = None
    blog_feed_url: Optional[str] = None
    industry: Optional[str] = None
    sic_code: Optional[str] = None
    employee_count: Optional[int] = None
    employee_count_at: Optional[str] = None
    hq_country: Optional[str] = None
    hq_region: Optional[str] = None
    hq_city: Optional[str] = None
    founded: Optional[str] = None
    company_type: Optional[str] = None
    cohort: Optional[str] = None
    seed_source: Optional[str] = None
    icp_fit: float = 1.0
    icp_reasons: list = field(default_factory=list)
    disqualified: bool = False
    disqualify_reason: Optional[str] = None
    score: Optional[float] = None
    tier: Optional[int] = None
    buying_window: Optional[str] = None
    scored_at: Optional[str] = None
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
    def from_db_row(cls, row: dict) -> "Account":
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
    domain: Optional[str] = None
    name: Optional[str] = None
    title: Optional[str] = None
    seniority: Optional[str] = None
    persona: Optional[str] = None
    department: Optional[str] = None
    linkedin_slug: Optional[str] = None
    linkedin_url: Optional[str] = None
    location: Optional[str] = None
    is_champion: bool = False
    role_started_at: Optional[str] = None
    prior_domain: Optional[str] = None
    email_guess: Optional[str] = None
    email_pattern: Optional[str] = None
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
    def from_db_row(cls, row: dict) -> "Contact":
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
    person_key: Optional[str] = None
    title: Optional[str] = None
    summary: Optional[str] = None
    evidence: Optional[str] = None
    evidence_data: dict = field(default_factory=dict)
    url: Optional[str] = None
    confidence: float = 0.8
    first_seen_at: Optional[str] = None
    last_seen_at: Optional[str] = None
    raw_ref: Optional[str] = None
    superseded_by: Optional[str] = None

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
    def from_db_row(cls, row: dict) -> "Signal":
        data = dict(row)
        data["evidence_data"] = _json_load(data.get("evidence_data"), {})
        return _row_to_dataclass(cls, data)


@dataclass
class Document:
    doc_id: str
    source: str
    url: Optional[str] = None
    domain: Optional[str] = None
    method: str = "GET"
    content_type: Optional[str] = None
    status: Optional[int] = None
    byte_size: Optional[int] = None
    path: Optional[str] = None
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    fetched_at: Optional[str] = None
    parsed_at: Optional[str] = None
    parse_error: Optional[str] = None
    body: Optional[bytes] = None  # NOT persisted; lives in rawstore

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
    def from_db_row(cls, row: dict) -> "Document":
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
    def from_db_row(cls, row: dict) -> "ScoreComponents":
        data = dict(row)
        data["per_signal"] = _json_load(data.get("per_signal"), [])
        data["combos"] = _json_load(data.get("combos"), [])
        return _row_to_dataclass(cls, data)
