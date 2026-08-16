"""Tests for SEC Form D funding parser."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from src.sources.sec.parse_formd import form_d_to_candidates, infer_round_stage, parse_form_d
from src.sources.sec.parse_submissions import Filing


FIXTURE = Path(__file__).parent / "fixtures" / "sec" / "form_d_primary_doc.xml"
FILING = Filing(
    accession="0001234567-26-000001",
    form="D",
    filing_date="2026-04-01",
    report_date=None,
    items=[],
    primary_document="primary_doc.xml",
    description="Form D",
    cik="0001234567",
)


def test_full_fixture_parses_fields():
    fd = parse_form_d(FIXTURE.read_bytes())
    assert fd.entity_name == "Acme Robotics Inc."
    assert fd.cik == "0001234567"
    assert fd.total_offering == 25_000_000.0
    assert fd.total_sold == 20_000_000.0
    assert fd.remaining == 5_000_000.0
    assert fd.date_of_first_sale == "2026-03-15"
    assert fd.industry_group == "Other Technology"
    assert fd.is_amendment is False
    assert "06b" in fd.exemptions
    assert fd.year_of_inc == "2021"
    assert fd.state == "DE"
    assert fd.related_persons[0]["name"] == "Jane Doe"
    assert "Executive Officer" in fd.related_persons[0]["relationship"]


def test_namespaced_and_plain_both_work():
    ns = FIXTURE.read_bytes()
    plain = ns.replace(b' xmlns="http://www.sec.gov/edgar/formd"', b"")
    a, b = parse_form_d(ns), parse_form_d(plain)
    assert a.entity_name == b.entity_name
    assert a.total_sold == b.total_sold


def test_missing_total_sold_confidence():
    xml = FIXTURE.read_text(encoding="utf-8").replace(
        "<totalAmountSold>20000000</totalAmountSold>", ""
    )
    fd = parse_form_d(xml.encode())
    assert fd.total_sold is None
    cands = form_d_to_candidates(fd, filing=FILING, today=date(2026, 8, 16))
    assert cands[0].confidence == 0.75


def test_amendment_no_new_money():
    xml = (
        FIXTURE.read_text(encoding="utf-8")
        .replace("<isAmendment>false</isAmendment>", "<isAmendment>true</isAmendment>")
        .replace("<totalAmountSold>20000000</totalAmountSold>", "<totalAmountSold>0</totalAmountSold>")
    )
    fd = parse_form_d(xml.encode())
    assert fd.is_amendment is True
    cands = form_d_to_candidates(fd, filing=FILING, today=date(2026, 8, 16))
    assert cands[0].confidence == 0.6


def test_round_stage_boundaries():
    assert infer_round_stage(2_999_999) == "Seed"
    assert infer_round_stage(3_000_000) == "Series A"
    assert infer_round_stage(14_999_999) == "Series A"
    assert infer_round_stage(15_000_000) == "Series B"
    assert infer_round_stage(49_999_999) == "Series B"
    assert infer_round_stage(50_000_000) == "Series C"
    assert infer_round_stage(150_000_000) == "Series C"
    assert infer_round_stage(150_000_001) == "Growth"


def test_observed_at_falls_back_to_filing_date():
    xml = FIXTURE.read_text(encoding="utf-8").replace(
        "<dateOfFirstSale><value>2026-03-15</value></dateOfFirstSale>",
        "<dateOfFirstSale></dateOfFirstSale>",
    )
    fd = parse_form_d(xml.encode())
    assert fd.date_of_first_sale is None
    cands = form_d_to_candidates(fd, filing=FILING, today=date(2026, 8, 16))
    assert cands[0].observed_at == "2026-04-01"
    assert cands[0].natural_key == FILING.accession
    assert cands[0].signal_type == "funding_form_d"
    assert cands[0].evidence_data["round_stage"] == "Series B"
