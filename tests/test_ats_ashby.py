from pathlib import Path
from src.sources.ats.ashby import parse_ashby

def test_parse_ashby():
    jobs = parse_ashby((Path(__file__).parent / "fixtures/ats/ashby_jobs.json").read_bytes())
    assert len(jobs) == 2
    paid = next(j for j in jobs if j.external_id == "a1")
    assert paid.comp_min == 100000
    assert paid.department == "Sales"
    assert paid.remote is True
    other = next(j for j in jobs if j.external_id == "a2")
    assert other.department is None
    assert other.country == "Canada"
