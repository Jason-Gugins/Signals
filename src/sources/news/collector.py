from datetime import date

from src.core.models import Account, Document
from src.sources.base import FetchTask, SignalCandidate, SourceAdapter
from src.sources.content.blog import blog_to_candidates
from src.sources.news.classify import classify_news
from src.sources.news.feeds import bing_news_url, google_news_search_url, google_news_topic_url, google_news_url, parse_feed
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
        if not doc.body:
            return []
        today = date.fromisoformat(task_meta["today"])
        out = []
        for it in parse_feed(doc.body):
            c = classify_news(it, account, today=today)
            if c:
                out.append(c)
        return out


@register
class CompanyFeedSource(SourceAdapter):
    key = "company_feed"
    tier = "http"
    cadence_hours = 24
    requires = ("blog_feed_url",)

    def plan(self, account, cursor):
        return [FetchTask(source=self.key, url=account.blog_feed_url, domain=account.domain)]

    def parse(self, doc, account, task_meta):
        if not doc.body:
            return []
        return blog_to_candidates(parse_feed(doc.body), account, today=date.fromisoformat(task_meta["today"]))


@register
class GoogleNewsSource(SourceAdapter):
    key = "google_news"
    tier = "http"
    cadence_hours = 12
    emits = ("funding_round", "exec_hire", "exec_departure", "product_launch",
             "layoff", "ma_acquirer", "ma_target", "ipo_filing", "ipo_pricing",
             "office_open", "award", "certification", "earnings_warning",
             "market_consolidation", "competitor_outage")

    # Default topic sections to fetch alongside the keyword search.
    DEFAULT_TOPICS = ("TECHNOLOGY", "BUSINESS")

    def plan(self, account, cursor):
        name = account.name or account.domain
        tasks = [
            FetchTask(
                source=self.key,
                url=google_news_search_url(f'"{name}"'),
                domain=account.domain,
                meta={"kind": "search", "query": name},
            ),
        ]
        for topic in self.DEFAULT_TOPICS:
            tasks.append(
                FetchTask(
                    source=self.key,
                    url=google_news_topic_url(topic),
                    domain=account.domain,
                    meta={"kind": "topic", "topic": topic},
                )
            )
        return tasks

    def parse(self, doc, account, task_meta):
        if not doc.body:
            return []
        today = date.fromisoformat(task_meta["today"])
        out = []
        for it in parse_feed(doc.body):
            c = classify_news(it, account, today=today)
            if c:
                out.append(c)
        return out
