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

    # Fetch budget per account: 1 homepage + len(FEED_GUESSES) guesses + 1 CDX
    # query + up to 2 wayback snapshots. A named constant prevents drift.
    MAX_FETCHES = 12

    def discover(self, account: Account) -> Optional[str]:
        if account.blog_feed_url:
            return None
        used = 0

        def fetch(url: str):
            nonlocal used
            if used >= self.MAX_FETCHES:
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
        # Wayback fallback: the live homepage may be a JS shell while an
        # archived snapshot still exposes <link rel="alternate"> tags. One
        # CDX query + at most two snapshot fetches (reuses the same budget).
        res = fetch(_wayback_cdx_url(account.domain))
        if res and res.ok and res.doc and res.doc.body:
            try:
                from src.sources.wayback.cdx import parse_cdx, pick_snapshots, snapshot_url

                rows = parse_cdx(res.doc.body)
                for ts in pick_snapshots(rows, per_year=1, max_total=2):
                    original = next(
                        (r.get("original") for r in rows if r.get("timestamp") == ts),
                        f"https://{account.domain}/",
                    )
                    snap = fetch(snapshot_url(ts, original))
                    if snap and snap.ok and snap.doc and snap.doc.body:
                        html = snap.doc.body.decode("utf-8", "replace")
                        found = discover_feeds(html, f"https://{account.domain}/")
                        # Only same-host feeds count — archived pages sometimes
                        # reference third-party widgets' feeds.
                        found = [f for f in found if _same_host(f, account.domain)]
                        if found:
                            account.blog_feed_url = found[0]
                            if self.registry:
                                self.registry.upsert(account, source="feed_discovery")
                            return found[0]
            except Exception as exc:
                from loguru import logger

                logger.warning("wayback feed hint failed for {}: {}", account.domain, exc)
        return None


def _wayback_cdx_url(domain: str) -> str:
    from src.sources.wayback.cdx import cdx_url

    return cdx_url(domain, from_year=2020, limit=50)


def _same_host(feed_url: str, domain: str) -> bool:
    try:
        from urllib.parse import urlparse

        host = (urlparse(feed_url).hostname or "").casefold().removeprefix("www.")
        return host == domain.casefold().removeprefix("www.")
    except ValueError:
        return False
