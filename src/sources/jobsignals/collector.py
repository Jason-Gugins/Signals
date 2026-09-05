"""Derive hiring signals from the jobs table. Local harvest only."""

from __future__ import annotations

import json
from datetime import timedelta

from src.core.models import Account
from src.core.textutil import slugify
from src.sources.base import SignalCandidate, SourceAdapter
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
        out = analyze_jobs(w, today=today, cfg=cfg)
        out.extend(self._backfill_cands(db, account, rows, today))
        return out

    def _backfill_cands(self, db, account, open_rows, today):
        """Recently-closed title that is open again -> backfill_open.

        A title closed within the last 120 days whose casefold-normalized
        form matches a currently-open title (same domain, typically a
        re-post under a new external_id) is a backfill. Monthly natural-key
        dedupe like the sibling types; one candidate per title even if the
        title was closed and re-posted several times in the window (most
        recent closure wins via ORDER BY closed_at DESC).
        """
        cutoff = (today - timedelta(days=120)).isoformat()
        closed = db.query(
            """SELECT title, closed_at FROM jobs
               WHERE domain=? AND closed_at IS NOT NULL AND closed_at >= ?
               ORDER BY closed_at DESC""",
            (account.domain, cutoff),
        )
        open_by_casefold = {
            str(r.get("title") or "").casefold(): r.get("title")
            for r in open_rows
            if r.get("title")
        }
        iso_month = today.strftime("%Y-%m")
        out = []
        seen = set()
        for r in closed:
            closed_title = r.get("title")
            if not closed_title:
                continue
            key = str(closed_title).casefold()
            open_title = open_by_casefold.get(key)
            if open_title is None or key in seen:
                continue
            seen.add(key)
            out.append(
                SignalCandidate(
                    "backfill_open",
                    today.isoformat(),
                    f"backfill:{account.domain}:{slugify(open_title)}:{iso_month}",
                    title=open_title,
                    confidence=0.6,
                    evidence_data={"title": open_title, "closed_at": r.get("closed_at")},
                )
            )
        return out
