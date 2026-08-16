"""Tests for SEC EDGAR CIK resolution (fixture-driven, no network)."""

from __future__ import annotations

import json
from pathlib import Path

from src.core.models import Account
from src.identity.edgar_ids import (
    match_cik,
    pad_cik,
    parse_company_tickers,
    parse_submissions_identity,
)


FIXTURE = Path(__file__).parent / "fixtures" / "sec" / "company_tickers.json"


def test_parse_company_tickers_five_entries_sorted():
    body = FIXTURE.read_bytes()
    rows = parse_company_tickers(body)
    assert len(rows) == 5
    assert [r["cik"] for r in rows] == sorted(r["cik"] for r in rows)
    apple = next(r for r in rows if r["ticker"] == "AAPL")
    assert apple == {"cik": "0000320193", "ticker": "AAPL", "name": "Apple Inc."}


def test_pad_cik():
    assert pad_cik(320193) == "0000320193"
    assert pad_cik("320193") == "0000320193"
    assert pad_cik("0000320193") == "0000320193"


def test_match_cik_ticker_wins():
    index = parse_company_tickers(FIXTURE.read_bytes())
    acct = Account(domain="apple.com", name="Something Else", ticker="AAPL")
    assert match_cik(acct, index) == "0000320193"


def test_match_cik_exact_name_ignores_legal_suffix():
    index = parse_company_tickers(FIXTURE.read_bytes())
    acct = Account(domain="apple.com", name="Apple")
    assert match_cik(acct, index) == "0000320193"


def test_match_cik_ambiguous_returns_none():
    index = [
        {"cik": "0000000001", "ticker": "AA", "name": "Acme Alpha"},
        {"cik": "0000000002", "ticker": "AB", "name": "Acme Beta"},
    ]
    acct = Account(domain="acme.com", name="Acme")
    assert match_cik(acct, index, min_similarity=0.4) is None


def test_parse_submissions_identity_extracts_sic():
    body = json.dumps(
        {
            "cik": "320193",
            "name": "Apple Inc.",
            "tickers": ["AAPL"],
            "sic": "3571",
            "sicDescription": "Electronic Computers",
            "stateOfIncorporation": "CA",
            "entityType": "operating",
            "website": "https://www.apple.com",
            "category": "Large accelerated filer",
            "fiscalYearEnd": "0926",
            "filings": {"recent": {}},
        }
    ).encode()
    got = parse_submissions_identity(body)
    assert got["cik"] == "0000320193"
    assert got["sic"] == "3571"
    assert got["sicDescription"] == "Electronic Computers"
    assert got["tickers"] == ["AAPL"]
    assert "filings" not in got
