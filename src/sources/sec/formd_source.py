"""Form D discovery adapter. plan() is a no-op; FundingTracker owns I/O."""

from __future__ import annotations

from datetime import date

from src.pipeline.funding import parse_funding_doc
from src.sources.base import SourceAdapter
from src.sources.registry import register
from src.sources.sec.formd_filter import FormDFilter


@register
class SecFormDSource(SourceAdapter):
    key = "sec_formd"
    tier = "http"
    cadence_hours = 24
    requires: tuple[str, ...] = ()
    # fanout: the tracker sweeps the WHOLE SEC Form D universe, so it plans
    # globally; a single-account flow must opt in with --include-fanout.
    fanout = True
    emits = ("funding_form_d",)

    def plan(self, account, cursor):
        return []

    def parse(self, doc, account, task_meta):
        today = date.fromisoformat(task_meta["today"])
        filt = task_meta.get("filt") or FormDFilter()
        if not isinstance(filt, FormDFilter):
            filt = FormDFilter(**filt) if isinstance(filt, dict) else FormDFilter()
        rows = parse_funding_doc(doc, task_meta, today=today, filt=filt)
        out = []
        for _fd, _filing, cand in rows:
            cand.domain_override = account.domain
            out.append(cand)
        return out
