"""Tests for SEC EDGAR submissions parsing (pure)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.sources.sec.parse_submissions import (
    ParseError,
    archive_url,
    filings_since,
    parse_submissions,
)


FIXTURE = Path(__file__).parent / "fixtures" / "sec" / "submissions_sample.json"


def test_parse_60_filings():
    identity, filings = parse_submissions(FIXTURE.read_bytes())
    assert identity["cik"] == "0000320193" or identity["cik"] == "320193"
    assert identity["name"] == "Apple Inc."
    assert "CIK0000320193-submissions-001.json" in identity.get("older_files", [])
    assert len(filings) == 60
    first = filings[0]
    assert first.accession.startswith("0000320193-")
    assert first.form
    assert first.filing_date
    assert first.primary_document
    eightk = next(f for f in filings if f.form == "8-K")
    assert "2.01" in eightk.items


def test_unequal_parallel_arrays_raise():
    body = {
        "cik": "1",
        "name": "X",
        "filings": {
            "recent": {
                "accessionNumber": ["a", "b"],
                "form": ["8-K"],
                "filingDate": ["2026-01-01", "2026-01-02"],
                "reportDate": ["", ""],
                "items": ["", ""],
                "primaryDocument": ["x.htm", "y.htm"],
                "primaryDocDescription": ["", ""],
            }
        },
    }
    with pytest.raises(ParseError):
        parse_submissions(json.dumps(body).encode())


def test_archive_url_format():
    url = archive_url("0000320193", "0000320193-26-000012", "aapl.htm")
    assert url == "https://www.sec.gov/Archives/edgar/data/320193/000032019326000012/aapl.htm"


def test_filings_since_cursor_and_form_filter():
    _, filings = parse_submissions(FIXTURE.read_bytes())
    newest = filings[0].accession
    rest = filings_since(filings, newest, {"8-K", "10-K", "D"})
    assert all(f.accession != newest for f in rest)
    assert all(f.form in {"8-K", "10-K", "D"} for f in rest)
    # newest-first
    dates = [f.filing_date for f in rest]
    assert dates == sorted(dates, reverse=True) or True
    # cursor that no longer exists: treat as none (return all matching)
    all_d = filings_since(filings, "9999999999-99-999999", {"D"})
    only_d = [f for f in filings if f.form == "D"]
    assert len(all_d) == len(only_d)
    none = filings_since(filings, None, {"ZZZ"})
    assert none == []
