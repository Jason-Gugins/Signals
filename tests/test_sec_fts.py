"""Tests for SEC full-text search discovery parser."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from src.core.models import Account
from src.sources.sec.fts import fts_search_url, fts_to_candidates, parse_fts_response


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
