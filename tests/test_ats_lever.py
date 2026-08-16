"""Tests for Lever job parser."""

from __future__ import annotations

from pathlib import Path

from src.sources.ats.lever import parse_lever


FIXTURE = Path(__file__).parent / "fixtures" / "ats" / "lever_postings.json"


def test_parse_lever_fixture():
    jobs = parse_lever(FIXTURE.read_bytes())
    assert len(jobs) >= 5
    by_id = {j.external_id: j for j in jobs}
    csm = by_id["lev-1"]
    assert csm.department == "CS"
    assert csm.posted_at is not None
    remote = by_id["lev-2"]
    assert remote.department is None
    assert remote.remote is True
    paid = by_id["lev-3"]
    assert paid.comp_min == 200000
    assert paid.comp_currency == "USD"
    assert paid.title == "Director of Engineering"
