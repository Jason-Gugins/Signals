"""Tests for SEC full-text search discovery parser."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from src.core.models import Account
from src.sources.sec.fts import (
    fts_search_url,
    fts_to_candidates,
    hit_to_filing,
    next_fts_offset,
    parse_fts_response,
    parse_fts_total,
)
from src.sources.sec.parse_submissions import archive_url


FIXTURE = Path(__file__).parent / "fixtures" / "sec" / "fts_response.json"


def test_parse_fts_response():
    hits = parse_fts_response(FIXTURE.read_bytes())
    assert len(hits) == 2
    assert hits[0]["accession"] == "0001868778-25-000019"
    assert hits[0]["cik"] == "0001868778"
    assert hits[0]["form"] == "8-K"
    assert hits[0]["filed"] == "2025-08-06"
    assert hits[0]["display_names"]


def test_fts_to_candidates_requires_name_in_display_names():
    hits = parse_fts_response(FIXTURE.read_bytes())
    acct = Account(domain="databricks.com", name="Databricks")
    cands = fts_to_candidates(hits, acct, today=date(2026, 8, 16))
    assert len(cands) == 1
    assert cands[0].signal_type == "ma_target"
    assert cands[0].confidence == 0.5
    assert cands[0].natural_key == "0000320193-26-000100"
    other = fts_to_candidates(hits, Account(domain="acme.com", name="Acme"), today=date(2026, 8, 16))
    assert other == []


def test_fts_search_url_recent_form_d():
    url = fts_search_url(forms=("D",), start="2026-07-23", end="2026-08-22", offset=0, size=10)
    assert url.startswith("https://efts.sec.gov/LATEST/search-index?")
    assert "forms=D" in url
    assert "dateRange=custom" in url
    assert "startdt=2026-07-23" in url
    assert "enddt=2026-08-22" in url
    assert "from=0" in url
    assert "size=10" in url
    assert "q=" not in url


def test_fts_search_url_search_encodes_q():
    url = fts_search_url(q="Other Technology", forms=("D",), start="2026-01-01", end="2026-08-22")
    assert "q=Other+Technology" in url or "q=Other%20Technology" in url
    assert "forms=D" in url


def test_fts_search_url_rejects_unknown_form_key():
    url = fts_search_url(q="Acme", forms=("D",))
    assert "entityName=" not in url
    assert "locationCodes=" not in url


FORMD_FIX = Path(__file__).parent / "fixtures" / "sec" / "fts_formd_recent.json"


def test_parse_fts_total_and_hits_are_form_d():
    body = FORMD_FIX.read_bytes()
    hits = parse_fts_response(body)
    total = parse_fts_total(body)
    assert total >= len(hits) >= 1
    assert total == 5007
    assert all(h["form"] in {"D", "D/A"} for h in hits)
    assert {h["form"] for h in hits} <= {"D", "D/A"}
    assert hits[0]["form"] == "D"


def test_parse_fts_total_bare_int():
    body = b'{"hits":{"total":12,"hits":[]}}'
    assert parse_fts_total(body) == 12


def test_next_fts_offset():
    assert next_fts_offset(offset=0, size=10, batch_len=10, total=25) == 10
    assert next_fts_offset(offset=20, size=10, batch_len=5, total=25) is None
    assert next_fts_offset(offset=0, size=10, batch_len=0, total=25) is None
    assert next_fts_offset(offset=0, size=100, batch_len=100, total=5007) == 100


def test_hit_to_filing_archive_url():
    hit = parse_fts_response(FORMD_FIX.read_bytes())[0]
    filing = hit_to_filing(hit)
    assert filing is not None
    assert filing.accession == hit["accession"]
    assert filing.cik == hit["cik"]
    assert filing.primary_document == "primary_doc.xml"
    assert filing.archive_url == archive_url(hit["cik"], hit["accession"], "primary_doc.xml")
    assert filing.archive_url == (
        f"https://www.sec.gov/Archives/edgar/data/{int(hit['cik'])}/"
        f"{hit['accession'].replace('-', '')}/primary_doc.xml"
    )


def test_hit_to_filing_missing_ids():
    assert hit_to_filing({"accession": "x", "cik": ""}) is None
    assert hit_to_filing({"accession": "", "cik": "0002151517"}) is None
    assert hit_to_filing({}) is None
