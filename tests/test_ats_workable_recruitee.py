from pathlib import Path
from src.sources.ats.workable import parse_workable
from src.sources.ats.recruitee import parse_recruitee

def test_parse_workable():
    jobs = parse_workable((Path(__file__).parent / "fixtures/ats/workable_jobs.json").read_bytes())
    assert len(jobs) == 2
    assert jobs[0].remote is True or next(j for j in jobs if j.external_id=="W1").remote

def test_parse_recruitee():
    jobs = parse_recruitee((Path(__file__).parent / "fixtures/ats/recruitee_jobs.json").read_bytes())
    assert len(jobs) == 2
    assert next(j for j in jobs if j.external_id=="1").department == "Design"
