"""Tests for the sec_formd adapter (plan is a no-op)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from src.core.models import Account, Document
from src.pipeline.funding import parse_funding_doc
from src.sources.registry import SOURCES
from src.sources.sec.formd_filter import FormDFilter
from src.sources.sec.formd_source import SecFormDSource


def test_sec_formd_registered_and_plan_empty():
    import src.sources  # noqa: F401

    assert "sec_formd" in SOURCES
    src = SecFormDSource()
    assert src.key == "sec_formd"
    assert src.plan(Account(domain="acme.com"), None) == []
    assert src.emits == ("funding_form_d",)


def test_parse_stamps_domain_override():
    xml = (Path(__file__).parent / "fixtures" / "sec" / "form_d_primary_doc.xml").read_bytes()
    doc = Document(doc_id="d", source="sec_formd", url="https://www.sec.gov/x/primary_doc.xml", body=xml)
    acct = Account(domain="acme.com")
    cands = SecFormDSource().parse(
        doc,
        acct,
        {"kind": "form_d", "today": "2026-08-22", "accession": "000-1", "cik": "0001234567", "filing_date": "2026-04-01"},
    )
    assert cands
    assert all(c.domain_override == "acme.com" for c in cands)


def test_parse_filt_dict_does_not_crash():
    xml = (Path(__file__).parent / "fixtures" / "sec" / "form_d_primary_doc.xml").read_bytes()
    doc = Document(doc_id="d", source="sec_formd", body=xml)
    cands = SecFormDSource().parse(
        doc,
        Account(domain="acme.com"),
        {
            "kind": "form_d",
            "today": "2026-08-22",
            "accession": "000-1",
            "cik": "0001234567",
            "filing_date": "2026-04-01",
            "filt": {"include_funds": True},
        },
    )
    assert isinstance(cands, list)
    _ = FormDFilter
    _ = parse_funding_doc
    _ = date
