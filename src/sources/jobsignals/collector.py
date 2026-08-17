"""Derive hiring signals from the jobs table. Local harvest only."""

from __future__ import annotations

import json
from datetime import timedelta

from src.core.models import Account
from src.sources.base import SourceAdapter
from src.sources.jobsignals.analyze import JobsWindow, analyze_jobs
from src.sources.jobsignals.thresholds import load_jobsignals_cfg
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
        rows = [dict(r) for r in db.query(
            "SELECT * FROM jobs WHERE domain=? AND closed_at IS NULL", (account.domain,)
        )]
        cutoff = (today - timedelta(days=90)).isoformat()
        prior = db.one(
            "SELECT * FROM job_snapshots WHERE domain=? AND as_of<=? ORDER BY as_of DESC",
            (account.domain, cutoff),
        )
        if prior is None:
            prior_depts = {r.get("department") for r in rows if r.get("department")}
            prior_ctys = {r.get("country") for r in rows if r.get("country")}
            baseline = {}
        else:
            by_dept = json.loads(prior["by_department"] or "{}")
            by_cty = json.loads(prior["by_country"] or "{}")
            prior_depts = {k for k, n in by_dept.items() if n and k != "unknown"}
            prior_ctys = {k for k, n in by_cty.items() if n and k != "unknown"}
            baseline = {k: int(v) for k, v in by_dept.items() if k != "unknown"}
        cfg = (task_meta or {}).get("jobsignals_cfg")
        if cfg is None:
            try:
                cfg = load_jobsignals_cfg()
            except Exception:
                cfg = {}
        w = JobsWindow(
            domain=account.domain,
            jobs=rows,
            prior_countries=prior_ctys,
            prior_departments=prior_depts,
            baseline_open_by_dept=baseline,
        )
        return analyze_jobs(w, today=today, cfg=cfg)
