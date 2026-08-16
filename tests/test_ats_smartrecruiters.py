from pathlib import Path
from src.sources.ats.smartrecruiters import parse_smartrecruiters, smartrecruiters_pages


def test_parse_smartrecruiters():
    jobs = parse_smartrecruiters((Path(__file__).parent / "fixtures/ats/smartrecruiters_jobs.json").read_bytes())
    assert len(jobs) == 2
    sm = next(j for j in jobs if j.external_id == "s1")
    assert sm.comp_min == 90000
    assert sm.department == "Sales"
    assert sm.city == "Austin"


def test_smartrecruiters_offsets():
    assert smartrecruiters_pages(0, 100) == []
    assert smartrecruiters_pages(1, 100) == [0]
    assert smartrecruiters_pages(100, 100) == [0]
    assert smartrecruiters_pages(101, 100) == [0, 100]
    assert smartrecruiters_pages(250, 100) == [0, 100, 200]
