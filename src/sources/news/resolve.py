"""Resolve the true publisher domain behind a SERP redirect link.

Three-tier strategy (offline-first, network is the exception):

1. Direct link (not news.google.com) → its own hostname.
2. Google News ``?url=`` query param or an http(s) URL found in the
   link/summary context (most Google News RSS items carry this).
3. Leftover ``news.google.com/rss/articles/<token>`` links → the module-level
   fallback (no injected resolver) always reports "cannot decode offline"
   (None); live google_news cycles get an injected :class:`DomainResolver`.

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
    """Deprecated shim: the decoder moved to src/sources/news/decode.py.

    The module-level fallback (no injected resolver) is OFFLINE-only now —
    it never decodes tokens. Live google_news cycles always get an injected
    DomainResolver from the runner.
    """
    return None


def _offline_domain(link: str, summary: str | None) -> str | None:
    """Offline tiers of the resolver — zero network.

    The own-host / ``?url=`` / summary-embedded-URL branches, verbatim from
    ``_resolve_publisher_domain_uncached``. Returns None when only a token
    decode (network) could decide; callers decide whether to pay for it.
    """
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
    except Exception:
        domain = None
    if domain:
        domain = domain.casefold()
        domain = domain.removeprefix("www.")
    return domain


def _resolve_publisher_domain_uncached(link: str, summary: str | None) -> str | None:
    domain = _offline_domain(link, summary)
    if domain is None:
        # Leftover token link: decode is the last resort (network; slow).
        try:
            if "/articles/" in urlparse(link).path:
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


class DomainResolver:
    """Per-cycle publisher resolver injected into google_news task meta.

    Order: offline tiers (zero network) -> durable sqlite cache (v8
    news_link_resolutions) -> bounded fetcher-based decode. Only successful
    decodes are cached; a token that failed once is memoized for the rest of
    the cycle (one decode attempt per token) and left free to retry next
    cycle. Never raises.
    """

    def __init__(self, fetcher, db, *, max_decodes: int = 8, now: str | None = None):
        self._fetcher = fetcher
        self._db = db
        self._budget = max_decodes
        self._now = now  # isoformat UTC; injected by the runner (UTC-clock tests)
        self._failed: set[str] = set()  # dead tokens seen this cycle: one attempt each

    def resolve(self, link: str, summary: str | None = None) -> str | None:
        try:
            domain = _offline_domain(link, summary)
            if domain:
                return domain
            from src.sources.news.decode import decode_token, extract_token

            token = extract_token(link)
            if not token:
                return None
            row = self._db.one(
                "SELECT domain FROM news_link_resolutions WHERE token = ?", (token,)
            )
            if row and row.get("domain"):
                return str(row["domain"])
            if self._budget <= 0:
                return None
            if token in self._failed:
                return None  # already cost its one decode attempt this cycle
            self._budget -= 1
            decoded = decode_token(self._fetcher, token)
            if not decoded:
                self._failed.add(token)
                return None
            host = _host(decoded)
            if not host or _is_google_news(host.casefold()):
                return None
            domain = host.casefold().removeprefix("www.")
            try:
                self._db.upsert(
                    "news_link_resolutions",
                    {"token": token, "domain": domain, "resolved_at": self._now},
                    pk=("token",),
                )
            except Exception:
                pass  # cache write is best-effort; attribution still returned
            return domain
        except Exception:
            return None
