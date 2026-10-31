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
from src.identity.names import name_similarity, normalize_name


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


def test_match_cik_single_fuzzy_survivor_at_default_attaches():
    # Fuzzy arm at the real default min_similarity=0.9 (no override): one
    # surviving CIK auto-accepts. Token sets {acme,alpha,beta,gamma} vs
    # {acme,alpha,beta} = 0.75 Jaccard + 0.15 prefix bonus = 0.90 exactly.
    # Plan: docs/plans/2026-10-30-name-resolution-tests.md Task 1.1.
    index = [{"cik": "0000000001", "ticker": "AAB", "name": "ACME ALPHA BETA INC"}]
    acct = Account(domain="acme.com", name="Acme Alpha Beta Gamma")
    assert name_similarity("Acme Alpha Beta Gamma", "ACME ALPHA BETA INC") >= 0.9
    assert match_cik(acct, index) == "0000000001"


def test_match_cik_just_below_fuzzy_threshold_misses():
    # Same index row, account name misses the 0.9 bar: 2/4 Jaccard + 0.08
    # first-token tie = 0.58 -> conservative None. Plan Task 1.2.
    index = [{"cik": "0000000001", "ticker": "AAB", "name": "ACME ALPHA BETA INC"}]
    acct = Account(domain="acme.com", name="Acme Alpha Gamma")
    assert name_similarity("Acme Alpha Gamma", "ACME ALPHA BETA INC") < 0.9
    assert match_cik(acct, index) is None


def test_match_cik_two_fuzzy_survivors_returns_none():
    # Never-guess: two index rows both clear 0.9 against one account name
    # (prefix bonus both ways — one row shorter, one longer) with distinct
    # CIKs -> None. Plan Task 1.3.
    index = [
        {"cik": "0000000001", "ticker": "AAB", "name": "ACME ALPHA BETA INC"},
        {"cik": "0000000002", "ticker": "AABG", "name": "ACME ALPHA BETA GAMMA DELTA INC"},
    ]
    acct = Account(domain="acme.com", name="Acme Alpha Beta Gamma")
    assert name_similarity("Acme Alpha Beta Gamma", "ACME ALPHA BETA INC") >= 0.9
    assert name_similarity(
        "Acme Alpha Beta Gamma", "ACME ALPHA BETA GAMMA DELTA INC"
    ) >= 0.9
    assert match_cik(acct, index) is None


def test_match_cik_generic_short_name_misses_conservatively():
    # "Acme" alone is too generic to attach to ACME GLOBAL HOLDINGS INC:
    # 1/3 Jaccard + 0.15 prefix bonus ~ 0.48, well under 0.9. Plan Task 1.4.
    index = [
        {"cik": "0000000001", "ticker": "AGH", "name": "ACME GLOBAL HOLDINGS INC"},
    ]
    acct = Account(domain="acme.com", name="Acme")
    assert name_similarity("Acme", "ACME GLOBAL HOLDINGS INC") < 0.9
    assert match_cik(acct, index) is None


def test_match_cik_ticker_collision_returns_none():
    # Ticker arm with two same-ticker rows / distinct CIKs -> None, and it
    # returns early without falling through to the (here unique) name arm.
    # Plan Task 1.5.
    index = [
        {"cik": "0000000001", "ticker": "ACME", "name": "Acme East Inc"},
        {"cik": "0000000002", "ticker": "ACME", "name": "Acme West Inc"},
    ]
    acct = Account(domain="acme.com", name="Acme East Inc", ticker="ACME")
    assert match_cik(acct, index) is None


def test_match_cik_degenerate_account_returns_none():
    # name=None + ticker=None: neither arm runs, None without a crash even
    # against a populated index. Plan Task 1.6.
    index = parse_company_tickers(FIXTURE.read_bytes())
    acct = Account(domain="acme.com")
    assert acct.name is None
    assert acct.ticker is None
    assert match_cik(acct, index) is None


def test_match_cik_punctuated_edgar_title_matches_via_exact_arm():
    # EDGAR titles carry punctuation ("Acme (Delaware)"); normalize_name
    # strips it, so the exact normalized-name arm attaches. Plan Task 1.7.
    index = [{"cik": "0000000001", "ticker": "ACME", "name": "Acme (Delaware)"}]
    acct = Account(domain="acme.com", name="Acme Delaware Inc")
    assert normalize_name("Acme (Delaware)") == normalize_name("Acme Delaware Inc")
    assert match_cik(acct, index) == "0000000001"


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
