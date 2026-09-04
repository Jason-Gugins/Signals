"""Resolve the true publisher domain behind a SERP redirect link.

Three-tier strategy (offline-first, network is the exception):

1. Direct link (not news.google.com) → its own hostname.
2. Google News ``?url=`` query param or an http(s) URL found in the
   link/summary context (most Google News RSS items carry this).
3. Leftover ``news.google.com/rss/articles/<token>`` links → decode via
   ``googlenewsdecoder`` (network; slow, ~1-2s per link) — resolved lazily
   and cached per link by :func:`resolve_publisher_domain`.

Resolution NEVER raises: any failure returns ``None`` so a parse can't
break over attribution.
"""
from __future__ import annotations

import functools
import re
from urllib.parse import parse_qs, urlparse

# Google News SERPs reuse the same article links across feeds/keywords, so a
# bounded LRU cache keeps the slow decoder to one call per unique link without
# unbounded process-lifetime growth in a long-running scheduler.
@functools.lru_cache(maxsize=4096)
def _cached_resolve(link: str, summary: str | None) -> str | None:
    return _resolve_publisher_domain_uncached(link, summary)


def _is_google_news(host: str) -> bool:
    return host == "news.google.com" or host.endswith(".news.google.com")


def _host(url: str) -> str | None:
    try:
        return urlparse(url).hostname
    except Exception:
        return None


def _resolve_google_news_token(link: str) -> str | None:
    """Decode a news.google.com/rss/articles/<token> link via googlenewsdecoder.

    The package pulls in requests/socks, so it is imported INSIDE this
    function body (never at module level) to keep feeds.py imports fast.
    A resolution failure returns None — never an exception.
    """
    try:
        from googlenewsdecoder import gnewsdecoder  # lazy: heavy transitive deps

        result = gnewsdecoder(link)
        if isinstance(result, dict) and result.get("status"):
            decoded = result.get("decoded_url") or ""
            host = _host(decoded)
            if host and not _is_google_news(host.casefold()):
                return host
    except Exception:
        pass
    return None


def _resolve_publisher_domain_uncached(link: str, summary: str | None) -> str | None:
    domain: str | None = None
    try:
        parsed = urlparse(link)
        host = (parsed.hostname or "").casefold()
        if host and not _is_google_news(host):
            domain = host
        else:
            # Google News link: prefer the explicit ?url= redirect target.
            qs = parse_qs(parsed.query)
            target = (qs.get("url") or [""])[0]
            cand_host = _host(target) if target.startswith("http") else None
            if cand_host and not _is_google_news(cand_host.casefold()):
                domain = cand_host
            else:
                # Try an http(s) URL embedded in the summary HTML (offline;
                # this re-scan overlaps _unwrap in feeds.py — cheap, left as is).
                if summary:
                    m = re.search(r"https?://[^\s\"'<>]+", summary)
                    if m:
                        cand_host = _host(m.group(0))
                        if cand_host and not _is_google_news(cand_host.casefold()):
                            domain = cand_host
                if domain is None and "/articles/" in parsed.path:
                    domain = _resolve_google_news_token(link)
    except Exception:
        domain = None
    if domain:
        domain = domain.casefold()
        domain = domain.removeprefix("www.")
    return domain


def resolve_publisher_domain(link: str, summary: str | None = None) -> str | None:
    """Best-effort publisher hostname for a news item's link.

    Returns the hostname casefolded with a leading ``www.`` stripped
    (e.g. ``"pymnts.com"``), or None when it cannot be determined.
    """
    if not link:
        return None
    return _cached_resolve(link, summary)
