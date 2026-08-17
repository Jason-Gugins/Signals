"""ATS adapter harvest_jobs wiring."""

from __future__ import annotations

from pathlib import Path

from src.core.models import Account, Document
from src.sources.ats.collector import (
    AshbySource,
    GreenhouseSource,
    LeverSource,
    RecruiteeSource,
    SmartRecruitersSource,
    WorkableSource,
    WorkdaySource,
)


def test_each_vendor_harvests_fixture():
    mapping = [
        (GreenhouseSource, "tests/fixtures/ats/greenhouse_jobs.json"),
        (LeverSource, "tests/fixtures/ats/lever_postings.json"),
        (AshbySource, "tests/fixtures/ats/ashby_jobs.json"),
        (SmartRecruitersSource, "tests/fixtures/ats/smartrecruiters_jobs.json"),
        (WorkableSource, "tests/fixtures/ats/workable_jobs.json"),
        (RecruiteeSource, "tests/fixtures/ats/recruitee_jobs.json"),
        (WorkdaySource, "tests/fixtures/ats/workday_jobs.json"),
    ]
    acct = Account(domain="acme.com", ats_token="acme")
    for cls, path in mapping:
        src = cls()
        assert src.key.startswith("ats_")
        doc = Document(doc_id="x", source=src.key, body=Path(path).read_bytes())
        jobs = src.harvest_jobs(doc, acct, {"today": "2026-08-16", "base": "https://wd.example/acme"})
        assert jobs, src.key
        assert src.parse(doc, acct, {"today": "2026-08-16"}) == []
