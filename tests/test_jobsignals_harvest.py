"""Tests appended for jobsignals local harvest."""

from datetime import date

from src.core.db import Database
from src.core.models import Account
from src.sources.ats.common import JobPost, snapshot_jobs, upsert_jobs
from src.sources.jobsignals.collector import JobSignalsSource


def _five_sales(db, domain="acme.com"):
    jobs = [
        JobPost(
            external_id=str(i),
            title="Account Executive",
            url=f"https://x/{i}",
            posted_at="2026-08-01",
            department="Sales",
            country="Canada",
        )
        for i in range(5)
    ]
    upsert_jobs(db, domain, jobs, "ats_greenhouse", now="2026-08-16T00:00:00", token="acme")
    return jobs


def test_local_harvest_reads_jobs_table(tmp_path):
    db = Database(tmp_path / "s.db")
    _five_sales(db)
    src = JobSignalsSource()
    cands = src.local_harvest(db=db, account=Account(domain="acme.com"), today=date(2026, 8, 16), task_meta={})
    assert any(c.signal_type == "hiring_surge" for c in cands)


def test_first_harvest_does_not_emit_expansion_or_new_geo(tmp_path):
    db = Database(tmp_path / "s.db")
    _five_sales(db)
    src = JobSignalsSource()
    cands = src.local_harvest(db=db, account=Account(domain="acme.com"), today=date(2026, 8, 16), task_meta={})
    types = {c.signal_type for c in cands}
    assert "department_expansion" not in types
    assert "new_geo" not in types
    assert "hiring_surge" in types


def test_second_snapshot_emits_new_department(tmp_path):
    db = Database(tmp_path / "s.db")
    _five_sales(db)
    snapshot_jobs(db, "acme.com", as_of="2026-05-01")
    extra = [
        JobPost(external_id="eng1", title="Backend Engineer", url="https://x/e", posted_at="2026-08-01", department="Engineering", country="Canada"),
        JobPost(external_id="eng2", title="Frontend Engineer", url="https://x/e2", posted_at="2026-08-01", department="Engineering", country="Canada"),
    ]
    upsert_jobs(db, "acme.com", extra, "ats_greenhouse", now="2026-08-16T00:00:00", token="acme")
    cands = JobSignalsSource().local_harvest(
        db=db, account=Account(domain="acme.com"), today=date(2026, 8, 16), task_meta={}
    )
    assert any(c.signal_type == "department_expansion" and "Engineering" in (c.title or "") for c in cands)
