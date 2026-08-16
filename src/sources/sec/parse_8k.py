"""PURE 8-K item classification and form-level mapping."""

from __future__ import annotations

import re
from datetime import date

from lxml import html

from src.core.textutil import clean_text
from src.sources.base import SignalCandidate
from src.sources.sec.parse_submissions import Filing

ITEM_MAP: dict[str, tuple[str | None, float]] = {
    "1.01": ("ma_acquirer", 0.45),
    "2.01": ("ma_acquirer", 0.9),
    "2.05": ("layoff", 0.85),
    "2.06": ("earnings_warning", 0.7),
    "5.02": ("exec_hire", 0.8),
    "7.01": (None, 0.0),
    "8.01": (None, 0.0),
}

_APPOINT = re.compile(r"\b(appoint(?:ed|ment)?|named|joins?|elected)\b", re.I)
_DEPART = re.compile(r"\b(resign(?:ed|ation)?|depart(?:ed|ure)?|step(?:s|ped)?\s+down|terminat)\b", re.I)
_PERSON = re.compile(
    r"\b(?:Mr|Ms|Mrs|Dr)\.?\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)(?:,\s+([^.]{3,60}))?",
)
_ACQUIRED = re.compile(
    r"acquisition of\s+([A-Z][\w .,&-]{2,60}?)(?:\s*,?\s*Inc\.?|\s+LLC|\.)",
    re.I,
)
_AFFECTED = re.compile(r"(\d[\d,]*)\s+(?:employees|roles|positions|workers)", re.I)
_CHARGE = re.compile(r"\$[\d,.]+\s*(?:million|billion|m|b)?", re.I)


def extract_text(html_bytes: bytes) -> str:
    doc = html.fromstring(html_bytes)
    text = doc.text_content()
    text = clean_text(text) or ""
    return text[:200_000]


def classify_form(filing: Filing, *, today: date) -> list[SignalCandidate]:
    form = filing.form
    mapping = {
        "S-1": ("ipo_filing", 0.9),
        "S-1/A": ("ipo_filing", 0.85),
        "424B4": ("ipo_pricing", 0.9),
        "10-K": ("annual_report_10k", 0.8),
        "425": ("ma_acquirer", 0.5),
    }
    if form == "25" or form not in mapping:
        return []
    typ, conf = mapping[form]
    return [
        SignalCandidate(
            signal_type=typ,
            observed_at=filing.filing_date,
            natural_key=filing.accession,
            title=f"{form} filed",
            url=filing.archive_url,
            confidence=conf,
            evidence_data={"form": form},
        )
    ]


def classify_8k(filing: Filing, body_text: str | None, *, today: date) -> list[SignalCandidate]:
    out: list[SignalCandidate] = []
    text = body_text or ""
    for item in filing.items:
        mapped = ITEM_MAP.get(item)
        if not mapped or mapped[0] is None:
            continue
        typ, conf = mapped
        if item == "5.02" and text:
            out.extend(_classify_502(filing, text, conf))
            continue
        data: dict = {"item": item}
        if item == "2.01" and text:
            m = _ACQUIRED.search(text)
            if m:
                data["acquired_company"] = m.group(1).strip()
        if item == "2.05" and text:
            am = _AFFECTED.search(text)
            if am:
                data["affected"] = am.group(1)
            ch = _CHARGE.search(text)
            if ch:
                data["charge_amount"] = ch.group(0)
        out.append(
            SignalCandidate(
                signal_type=typ,
                observed_at=filing.filing_date,
                natural_key=f"{filing.accession}:{item}",
                title=f"8-K item {item}",
                url=filing.archive_url,
                confidence=conf,
                evidence_data=data,
            )
        )
    return out


def _classify_502(filing: Filing, text: str, conf: float) -> list[SignalCandidate]:
    hire = bool(_APPOINT.search(text))
    leave = bool(_DEPART.search(text))
    types: list[str] = []
    if hire:
        types.append("exec_hire")
    if leave:
        types.append("exec_departure")
    if not types:
        types.append("exec_hire")
    person = None
    role = None
    m = _PERSON.search(text)
    if m:
        person = m.group(1)
        role = (m.group(2) or "").strip(" ,") or None
        if role and re.search(r"\bappointed\b", role, re.I):
            role = None
    # better role: "Jane Doe, Chief Revenue Officer, was appointed"
    m2 = re.search(
        r"(?:Mr|Ms|Mrs|Dr)\.?\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+),\s+([^,]{3,50}),\s+was\s+appointed",
        text,
    )
    if m2:
        person, role = m2.group(1), m2.group(2).strip()
    out = []
    for typ in types:
        out.append(
            SignalCandidate(
                signal_type=typ,
                observed_at=filing.filing_date,
                natural_key=f"{filing.accession}:5.02:{typ}",
                title=f"{person or 'Officer'} {typ.replace('_', ' ')}",
                url=filing.archive_url,
                confidence=conf,
                evidence_data={"item": "5.02", "person_name": person, "new_role": role},
            )
        )
    return out
