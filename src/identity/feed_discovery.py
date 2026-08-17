"""Discover a company blog RSS/Atom URL. discover_feeds is pure; I/O lives here."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from src.core.models import Account
from src.sources.news.feeds import FEED_GUESSES, discover_feeds, parse_feed

if TYPE_CHECKING:
    from src.core.http import HttpFetcher
    from src.identity.registry import AccountRegistry


class FeedDiscovery:
    def __init__(self, fetcher: "HttpFetcher", registry: "AccountRegistry | None"):
        self.fetcher = fetcher
        self.registry = registry

    def discover(self, account: Account) -> Optional[str]:
        if account.blog_feed_url:
            return None
        used = 0

        def fetch(url: str):
            nonlocal used
            if used >= 4:
                return None
            used += 1
            from src.identity.edgar_ids import _Task

            return self.fetcher.get(_Task(source="feed_discovery", url=url, domain=account.domain))

        home = f"https://{account.domain}/"
        res = fetch(home)
        if res and res.ok and res.doc and res.doc.body:
            html = res.doc.body.decode("utf-8", "replace")
            found = discover_feeds(html, home)
            if found:
                account.blog_feed_url = found[0]
                if self.registry:
                    self.registry.upsert(account, source="feed_discovery")
                return found[0]
        for path in FEED_GUESSES:
            url = f"https://{account.domain}{path}"
            res = fetch(url)
            if not res or not res.ok or not res.doc or not res.doc.body:
                continue
            if parse_feed(res.doc.body):
                account.blog_feed_url = url
                if self.registry:
                    self.registry.upsert(account, source="feed_discovery")
                return url
        return None
