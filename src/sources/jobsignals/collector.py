"""Derive hiring signals from the jobs table. Local harvest only."""

from __future__ import annotations

from src.core.models import Account
from src.sources.base import SourceAdapter
from src.sources.jobsignals.analyze import JobsWindow, analyze_jobs
from src.sources.registry import register


@register
class JobSignalsSource(SourceAdapter):
    key = "jobsignals"
    tier = "local"
    cadence_hours = 12

    def plan(self, account: Account, cursor):
        return []

    def parse(self, doc, account, task_meta):
        return []

    def local_harvest(self, *, db, account, today, task_meta):
        rows = db.query("SELECT * FROM jobs WHERE domain=?", (account.domain,))
        w = JobsWindow(
            domain=account.domain,
            jobs=[dict(r) for r in rows],
            prior_countries=set(),
            prior_departments=set(),
            baseline_open_by_dept={},
        )
        return analyze_jobs(w, today=today, cfg={})
