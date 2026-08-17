from src.sources.base import FetchTask, SourceAdapter
from src.sources.content.itunes import itunes_url
from src.sources.registry import register


@register
class ContentItunesSource(SourceAdapter):
    key = "content_itunes"
    tier = "http"
    cadence_hours = 168

    def plan(self, account, cursor):
        return [FetchTask(source=self.key, url=itunes_url(account.name or account.domain), domain=account.domain)]

    def parse(self, doc, account, task_meta):
        return []
