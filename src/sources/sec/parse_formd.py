"""PURE Form D XML parser. Namespace-agnostic."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from lxml import etree

from src.identity.edgar_ids import pad_cik
from src.sources.base import SignalCandidate
from src.sources.sec.parse_submissions import Filing


@dataclass(frozen=True)
class FormD:
    entity_name: str
    cik: str
    total_offering: float | None
    total_sold: float | None
    remaining: float | None
    date_of_first_sale: str | None
    industry_group: str | None
    is_amendment: bool
    exemptions: list[str]
    year_of_inc: str | None
    state: str | None
    related_persons: list[dict]


def _strip_ns(root: etree._Element) -> etree._Element:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _text(el: etree._Element | None) -> str | None:
    if el is None or el.text is None:
        return None
    s = " ".join(el.text.split())
    return s or None


def _first(root: etree._Element, xpath: str) -> etree._Element | None:
    found = root.xpath(xpath)
    return found[0] if found else None


def _money(el: etree._Element | None) -> float | None:
    t = _text(el)
    if t is None:
        return None
    try:
        return float(t.replace(",", ""))
    except ValueError:
        return None


def parse_form_d(xml_bytes: bytes) -> FormD:
    root = etree.fromstring(xml_bytes)
    _strip_ns(root)
    name = _text(_first(root, ".//entityName")) or ""
    cik_raw = _text(_first(root, ".//cik")) or "0"
    sold = _money(_first(root, ".//totalAmountSold"))
    offering = _money(_first(root, ".//totalOfferingAmount"))
    remaining = _money(_first(root, ".//totalRemaining"))
    first_sale = _text(_first(root, ".//dateOfFirstSale/value"))
    industry = _text(_first(root, ".//industryGroupType"))
    amend_el = _first(root, ".//isAmendment")
    is_amendment = (_text(amend_el) or "").lower() in {"true", "1", "yes"}
    exemptions = [t for t in (_text(x) for x in root.xpath(".//federalExemptionsExclusions/item")) if t]
    year = _text(_first(root, ".//yearOfInc/value"))
    state = _text(_first(root, ".//jurisdictionOrganization"))
    people = []
    for person in root.xpath(".//relatedPersonInfo"):
        first = _text(_first(person, ".//firstName")) or ""
        last = _text(_first(person, ".//lastName")) or ""
        rels = [t for t in (_text(x) for x in person.xpath(".//relationship")) if t]
        people.append({"name": f"{first} {last}".strip(), "relationship": rels})
    return FormD(
        entity_name=name,
        cik=pad_cik(cik_raw),
        total_offering=offering,
        total_sold=sold,
        remaining=remaining,
        date_of_first_sale=first_sale,
        industry_group=industry,
        is_amendment=is_amendment,
        exemptions=exemptions,
        year_of_inc=year,
        state=state,
        related_persons=people,
    )


def infer_round_stage(amount: float | None) -> str | None:
    if amount is None:
        return None
    if amount < 3_000_000:
        return "Seed"
    if amount < 15_000_000:
        return "Series A"
    if amount < 50_000_000:
        return "Series B"
    if amount <= 150_000_000:
        return "Series C"
    return "Growth"


def _fmt_amount(n: float | None) -> str:
    if n is None:
        return ""
    if n >= 1_000_000_000:
        return f"${n / 1_000_000_000:.1f}B".replace(".0B", "B")
    if n >= 1_000_000:
        val = n / 1_000_000
        return f"${val:.1f}M".replace(".0M", "M")
    return f"${n:,.0f}"


def form_d_to_candidates(fd: FormD, *, filing: Filing, today: date) -> list[SignalCandidate]:
    amount = fd.total_sold if fd.total_sold not in (None, 0.0) else fd.total_offering
    stage = infer_round_stage(amount)
    if fd.total_sold not in (None, 0.0):
        conf = 0.95
    elif fd.is_amendment:
        conf = 0.6
    else:
        conf = 0.75
    observed = fd.date_of_first_sale or filing.filing_date
    return [
        SignalCandidate(
            signal_type="funding_form_d",
            observed_at=observed,
            natural_key=filing.accession,
            title=f"{fd.entity_name} Form D {stage or ''}".strip(),
            url=filing.archive_url,
            confidence=conf,
            evidence_data={
                "amount_display": _fmt_amount(amount),
                "amount_usd": amount,
                "round_stage": stage,
                "round_stage_basis": "form_d_amount_heuristic",
                "first_sale": fd.date_of_first_sale,
                "exemption": ",".join(fd.exemptions),
                "is_amendment": fd.is_amendment,
            },
        )
    ]
