"""Parse live-captured ATS JSON when recon left a fixture. Missing files skip."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from src.sources.ats.ashby import parse_ashby
from src.sources.ats.recruitee import parse_recruitee
from src.sources.ats.workable import parse_workable
from src.sources.ats.workday import parse_workday

FIX = Path(__file__).parent / "fixtures" / "ats"


def _maybe(name: str) -> Path | None:
    p = FIX / name
    return p if p.exists() else None


@pytest.mark.parametrize(
    "fname,parser",
    [
        ("ashby_live.json", parse_ashby),
        ("workable_live.json", parse_workable),
        ("recruitee_live.json", parse_recruitee),
    ],
)
def test_live_json_parses(fname, parser):
    p = _maybe(fname)
    if p is None:
        pytest.skip(f"no live fixture {fname}")
    jobs = parser(p.read_bytes())
    assert jobs
    assert jobs[0].title and jobs[0].external_id


def test_workday_live_json():
    p = _maybe("workday_live.json")
    if p is None:
        pytest.skip("no live workday fixture")
    jobs = parse_workday(p.read_bytes(), base="https://example.wd5.myworkdayjobs.com", today=date(2026, 8, 16))
    assert isinstance(jobs, list)
