"""WARN notices fanout adapter."""

from __future__ import annotations

from datetime import date

from src.sources.base import FetchTask, SourceAdapter
from src.sources.registry import register
from src.sources.warn import match_notices, warn_to_candidate
from src.sources.warn.collector import WARN_JURISDICTIONS


@register
class WarnNoticesSource(SourceAdapter):
    key = "warn_notices"
    tier = "http"
    cadence_hours = 24
    # fanout: one index per jurisdiction is swept globally, so a
    # single-account flow must opt in with --include-fanout.
    fanout = True

    def plan(self, account, cursor):
        tasks = []
        for code in ("NY", "CA"):
            jur = WARN_JURISDICTIONS[code]
            tasks.append(FetchTask(source=self.key, url=jur.index_url, domain=None, meta={"state": code}))
        return tasks

    def parse(self, doc, account, task_meta):
        if not doc.body:
            return []
        today = date.fromisoformat(task_meta["today"])
        state = (task_meta or {}).get("state") or "NY"
        jur = WARN_JURISDICTIONS.get(state)
        if jur is None:
            return []
        notices = jur.parse(doc.body)
        registry = task_meta.get("registry")
        if registry is None:
            return [warn_to_candidate(n, today=today) for n in notices]
        matched = match_notices(notices, registry)
        out = []
        for notice, domain in matched:
            cand = warn_to_candidate(notice, today=today)
            cand.domain_override = domain
            out.append(cand)
        return out
