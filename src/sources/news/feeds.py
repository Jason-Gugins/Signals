"""Feed discovery and RSS/Atom parsing. PURE besides feedparser."""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape
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


_TAG = re.compile(r"<[^>]+>")
_DROP = re.compile(r"<(script|style)\b.*?</\1>", re.I | re.S)
_BREAK = re.compile(r"</?(p|div|li|br|tr|h[1-6])\b[^>]*>", re.I)


def html_to_text(html: str) -> str:
    """Crude, pure HTML -> text: drops script/style, keeps readable words.

    Deliberately not a full parser: the consumer is sentence extraction, which
    only needs prose. Stdlib only, never raises, and idempotent on plain text
    (a string with no '<' comes back unchanged apart from whitespace collapse).
    """
    if not html:
        return ""
    text = _DROP.sub(" ", str(html))
    text = _BREAK.sub("\n", text)
    text = _TAG.sub("", text)
    text = unescape(text)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


@dataclass(frozen=True)
class NewsItem:
    title: str
    link: str
    published: str | None
    summary: str | None
    source_name: str | None
    publisher_domain: str | None = None


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


GOOGLE_NEWS_TOPICS = (
    "WORLD", "NATION", "BUSINESS", "TECHNOLOGY",
    "ENTERTAINMENT", "SCIENCE", "SPORTS", "HEALTH",
)


def google_news_search_url(query: str, *, days: int = 30, lang: str = "en-US", country: str = "US") -> str:
    """Build a Google News RSS search-by-keyword URL.

    Includes a ``when:{days}d`` date window for parity with
    ``google_news_url`` — without it, Google returns articles of any age
    and ``classify_news`` has no effective date filter (``to_iso_date``
    does not parse RFC 822 pubDate strings, so ``published`` is always None).
    """
    q = quote_plus(query) + f"+when:{days}d"
    return f"https://news.google.com/rss/search?q={q}&hl={lang}&gl={country}&ceid={country}:{lang.split('-')[0]}"


def google_news_topic_url(topic: str, *, lang: str = "en-US", country: str = "US") -> str:
    """Build a Google News RSS section/topic URL (TECHNOLOGY, BUSINESS, etc.).

    For a specific topic ID copied from news.google.com, pass the ID string
    directly — this function handles named sections only.
    """
    t = topic.upper().strip()
    if t in GOOGLE_NEWS_TOPICS:
        base = f"https://news.google.com/rss/headlines/section/topic/{t}"
    else:
        # Treat as a raw topic ID (e.g. "CAAqJggKIiBDQkFTRWdvSUwyMHZNRGRqTVhZ")
        base = f"https://news.google.com/rss/topics/{topic}"
    return f"{base}?hl={lang}&gl={country}&ceid={country}:{lang.split('-')[0]}"


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
        # Resolve the true publisher domain (offline-first: ?url= param or
        # summary link; only leftover news.google.com tokens hit the slow
        # decoder — cached per link there). Failure → None, never a crash.
        from src.sources.news.resolve import resolve_publisher_domain

        out.append(NewsItem(
            title=title, link=link, published=published, summary=summary,
            source_name=source, publisher_domain=resolve_publisher_domain(link, summary),
        ))
    return out
