from datetime import date

from src.core.models import Account, Document
from src.sources.base import FetchTask, SourceAdapter
from src.sources.registry import register
from src.sources.regulatory.federal_register import (
    fr_query_url,
    fr_to_candidates,
    match_watches,
    parse_fr_documents,
)


@register
class FederalRegisterSource(SourceAdapter):
    key = "federal_register"
    tier = "http"
    cadence_hours = 24
    fanout = True

    def plan(self, account, cursor):
        since = cursor or "2026-01-01"
        return [FetchTask(source=self.key, url=fr_query_url(["consumer privacy"], since), domain=account.domain)]

    def parse(self, doc, account, task_meta):
        if not doc.body:
            return []
        today = date.fromisoformat(task_meta["today"])
        watches = task_meta.get("watches") or []
        out = []
        for item in parse_fr_documents(doc.body):
            ids = match_watches(item, watches)
            out.extend(fr_to_candidates(item, ids, account, today=today, watches=watches))
        return out
