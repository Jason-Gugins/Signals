from src.sources.base import FetchTask, SourceAdapter
from src.sources.registry import register
from src.sources.wayback.cdx import cdx_url


@register
class WaybackSource(SourceAdapter):
    key = "wayback"
    tier = "http"
    cadence_hours = 168

    def plan(self, account, cursor):
        return [FetchTask(source=self.key, url=cdx_url(account.domain, from_year=2018), domain=account.domain)]

    def parse(self, doc, account, task_meta):
        return []
