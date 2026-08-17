"""Feed discovery and RSS/Atom parsing. PURE besides feedparser."""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlparse, quote_plus

import feedparser

from src.core.textutil import to_iso_date


FEED_GUESSES = [
    "/feed",
    "/rss",
    "/rss.xml",
    "/atom.xml",
    "/index.xml",
    "/blog/feed",
    "/blog/rss.xml",
    "/news/rss",
    "/press/feed",
]


@dataclass(frozen=True)
class NewsItem:
    title: str
    link: str
    published: str | None
    summary: str | None
    source_name: str | None


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.feeds: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag != "link":
            return
        ad = dict(attrs)
        typ = (ad.get("type") or "").lower()
        if typ in {"application/rss+xml", "application/atom+xml"} and ad.get("href"):
            self.feeds.append(ad["href"])


def discover_feeds(html: str, base_url: str) -> list[str]:
    p = _LinkParser()
    p.feed(html)
    seen = []
    host = urlparse(base_url).hostname or ""
    abs_urls = []
    for href in p.feeds:
        abs_urls.append(urljoin(base_url, href))
    def rank(u: str) -> tuple:
        h = urlparse(u).hostname or ""
        path = urlparse(u).path or ""
        return (0 if h == host else 1, 0 if "/blog" in path else 1, u)
    for u in sorted(abs_urls, key=rank):
        if u not in seen:
            seen.append(u)
    return seen


def google_news_url(name: str, *, days: int = 30, lang: str = "en-US", country: str = "US") -> str:
    q = quote_plus(f'"{name}"') + f"+when:{days}d"
    return f"https://news.google.com/rss/search?q={q}&hl={lang}&gl={country}&ceid={country}:{lang.split('-')[0]}"


def bing_news_url(name: str) -> str:
    return f"https://www.bing.com/news/search?q={quote_plus(name)}&format=rss"


def _unwrap(link: str, summary: str | None) -> str:
    parsed = urlparse(link)
    qs = parse_qs(parsed.query)
    if "url" in qs and qs["url"][0].startswith("http"):
        return qs["url"][0]
    if summary:
        import re
        m = re.search(r"https?://[^\s\"'<>]+", summary)
        if m and "news.google.com" not in m.group(0):
            return m.group(0)
    return link


def parse_feed(body: bytes) -> list[NewsItem]:
    try:
        parsed = feedparser.parse(body)
    except Exception:
        return []
    if getattr(parsed, "bozo", False) and not parsed.entries:
        return []
    out = []
    for e in parsed.entries:
        title = (e.get("title") or "").strip()
        link = (e.get("link") or "").strip()
        if not title or not link:
            continue
        published = to_iso_date(e.get("published") or e.get("updated"))
        summary = e.get("summary") or e.get("description")
        source = None
        if e.get("source"):
            source = e.source.get("title") if hasattr(e.source, "get") else getattr(e.source, "title", None)
        link = _unwrap(link, summary)
        out.append(NewsItem(title=title, link=link, published=published, summary=summary, source_name=source))
    return out
