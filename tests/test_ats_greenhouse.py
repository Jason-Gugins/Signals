"""Tests for Greenhouse job parser."""

from __future__ import annotations

from pathlib import Path

from src.sources.ats.greenhouse import parse_greenhouse


FIXTURE = Path(__file__).parent / "fixtures" / "ats" / "greenhouse_jobs.json"


def test_parse_greenhouse_fixture():
    jobs = parse_greenhouse(FIXTURE.read_bytes())
    assert len(jobs) >= 5
    by_id = {j.external_id: j for j in jobs}
    ae = by_id["101"]
    assert ae.department == "Sales"
    assert ae.city == "Toronto"
    assert ae.country == "Canada"
    assert "Sell our product" in (ae.description or "")
    assert "&lt;" not in (ae.description or "")
    assert "&amp;" not in (ae.description or "")
    eng = by_id["102"]
    assert eng.department is None
    assert eng.remote is True
    paid = by_id["105"]
    assert paid.comp_min == 120000.0
    assert paid.comp_currency == "USD"
