from src.core.models import Account, Document
from src.sources.base import FetchTask, SignalCandidate, SourceAdapter
from src.sources.news.feeds import bing_news_url, google_news_url
from src.sources.registry import register


@register
class NewsRssSource(SourceAdapter):
    key = "news_rss"
    tier = "http"
    cadence_hours = 12

    def plan(self, account, cursor):
        name = account.name or account.domain
        return [
            FetchTask(source=self.key, url=google_news_url(name), domain=account.domain, meta={"kind": "gnews"}),
            FetchTask(source=self.key, url=bing_news_url(name), domain=account.domain, meta={"kind": "bing"}),
        ]

    def parse(self, doc, account, task_meta):
        return []
