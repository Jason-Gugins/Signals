from src.core.models import Account, Document
from src.sources.base import FetchTask, SourceAdapter
from src.sources.registry import register
from src.sources.regulatory.federal_register import fr_query_url


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
        return []
