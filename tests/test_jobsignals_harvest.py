"""Tests appended for jobsignals local harvest."""

from datetime import date

from src.core.db import Database
from src.core.models import Account
from src.sources.ats.common import JobPost, upsert_jobs
from src.sources.jobsignals.collector import JobSignalsSource


def test_local_harvest_reads_jobs_table(tmp_path):
    db = Database(tmp_path / "s.db")
    jobs = [
        JobPost(
            external_id=str(i),
            title="Account Executive",
            url=f"https://x/{i}",
            posted_at="2026-08-01",
            department="Sales",
        )
        for i in range(5)
    ]
    upsert_jobs(db, "acme.com", jobs, "ats_greenhouse", now="2026-08-16T00:00:00", token="acme")
    src = JobSignalsSource()
    cands = src.local_harvest(db=db, account=Account(domain="acme.com"), today=date(2026, 8, 16), task_meta={})
    assert any(c.signal_type == "hiring_surge" for c in cands)
