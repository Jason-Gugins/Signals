"""Tests for follow_funding + parse_funding_doc (pure)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from src.core.models import Document
from src.pipeline.funding import SOURCE, follow_funding, parse_funding_doc
from src.sources.sec.formd_filter import FormDFilter
from src.sources.sec.fts import hit_to_filing, parse_fts_response


FIX = Path(__file__).resolve().parent / "fixtures"
TODAY = date(2026, 8, 22)


def _doc(body: bytes, url: str = "https://example.test/x") -> Document:
    return Document(doc_id="d", source=SOURCE, url=url, body=body)


def test_follow_fts_emits_form_d_and_caps_limit():
    body = (FIX / "sec" / "fts_formd_recent.json").read_bytes()
    hits = parse_fts_response(body)
    follows = follow_funding(
        _doc(body),
        {"kind": "fts", "today": TODAY.isoformat(), "limit": 2},
        filt=FormDFilter(),
    )
    assert len(follows) == 2
    assert all(t.source == SOURCE and t.meta.get("kind") == "form_d" for t in follows)
    first = hit_to_filing(hits[0])
    assert first is not None
    assert follows[0].url == first.archive_url
    assert follows[0].meta.get("accession") == first.accession


def test_follow_submissions_skips_amendments_by_default():
    body = (FIX / "sec" / "submissions_sample.json").read_bytes()
    meta = {"kind": "submissions", "today": TODAY.isoformat(), "limit": 100}
    no_da = follow_funding(_doc(body), meta, filt=FormDFilter())
    with_da = follow_funding(_doc(body), meta, filt=FormDFilter(include_amendments=True))
    assert no_da
    assert all("/primary_doc.xml" in t.url for t in no_da)
    assert all(t.meta.get("kind") == "form_d" for t in no_da)
    assert len(with_da) > len(no_da)


def test_follow_form_d_is_terminal():
    body = (FIX / "sec" / "form_d_primary_doc.xml").read_bytes()
    assert follow_funding(_doc(body), {"kind": "form_d"}, filt=FormDFilter()) == []


def test_parse_form_d_emits_candidate():
    body = (FIX / "sec" / "form_d_primary_doc.xml").read_bytes()
    rows = parse_funding_doc(
        _doc(body, "https://www.sec.gov/Archives/edgar/data/1234567/x/primary_doc.xml"),
        {"kind": "form_d", "accession": "0001234567-26-000001", "cik": "0001234567", "filing_date": "2026-04-01"},
        today=TODAY,
        filt=FormDFilter(),
    )
    assert len(rows) == 1
    fd, filing, cand = rows[0]
    assert fd.entity_name == "Acme Robotics Inc."
    assert cand.signal_type == "funding_form_d"
    assert cand.domain_override is None
    assert filing.accession == "0001234567-26-000001"


def test_parse_drops_pooled_and_non_form_d_kind():
    xml = (FIX / "sec" / "form_d_primary_doc.xml").read_text(encoding="utf-8")
    xml = xml.replace("Other Technology", "Pooled Investment Fund")
    rows = parse_funding_doc(
        _doc(xml.encode()),
        {"kind": "form_d", "accession": "a", "cik": "0001234567", "filing_date": "2026-04-01"},
        today=TODAY,
        filt=FormDFilter(),
    )
    assert rows == []
    fts = parse_funding_doc(
        _doc((FIX / "sec" / "fts_formd_recent.json").read_bytes()),
        {"kind": "fts"},
        today=TODAY,
        filt=FormDFilter(),
    )
    assert fts == []
