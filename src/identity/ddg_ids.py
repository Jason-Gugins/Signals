"""Discover company domains from the DuckDuckGo HTML SERP (curl tier).

Waterfall stage 4 of the Clay "find the company" clone (plan T3c). Intended
dispatch: wired into the resolve waterfall by the parent as a name->domain
resolver (this module does not self-dispatch). Exactly ONE GET per name via
the chrome-TLS curl_cffi tier verified by the T1 probe
(data/probe/KEYLESS_IDENTITY_2026_09.md rung 5); the plain httpx tier is
dead (HTTP 202 + challenge body) and is deliberately not built.

ROBOTS EXCEPTION — read before changing the fetch behavior: duckduckgo.com
robots.txt disallows scraping and CurlCffiFetcher has no robots logic. This
resolver is the documented, deliberate exception: it runs resolve-time only
(never on a collection cadence), ships off by default behind a wiring flag
(added by the parent wiring task), is self-paced (DDG_PACE_S minimum seconds
between actual network calls) and issues one request per lookup. The README
paragraph owning this exception lands with the docs task (plan T13).

Body-validated, never status-validated: DDG serves challenge pages with
HTTP 200 AND 202 alike, so every response body is scanned for challenge
markers before parsing and success is defined by ``result__a`` anchors, not
the status code. A challenge body becomes status "error" with the marker
recorded — the stage no-ops, it does not become a no_match.

Parsers/picker are pure; ranking is display-only. The single strict
auto-accept is a #1-ranked result whose domain matches the name AND which
strictly outscores every other candidate — everything else comes back as
ranked candidates for the review queue (never-guess contract).
"""

from __future__ import annotations

import re
import time
from typing import Callable, Optional, Union
from urllib.parse import parse_qs, quote_plus, unquote, urlsplit

from loguru import logger

from src.identity.domains import MULTI_LABEL_TLDS, root_domain
from src.identity.names import name_matches_domain, name_tokens

DDG_HTML_URL = "https://html.duckduckgo.com/html/"

# Minimum seconds between actual network calls (self-paced; the curl tier has
# no RateLimiter). Last-request bookkeeping is instance-level with injected
# clock/sleep (TokenBucket precedent), so tests stay offline and instant.
DDG_PACE_S = 5.0

# Body-validation markers (case-insensitive scan). T1 probe: the plain tier
# served "Unfortunately, bots use DuckDuckGo too." with HTTP 202, and DDG
# ships challenge pages with 200 as well — the body is authoritative. Most
# specific marker first: the recorded marker is the first hit.
_CHALLENGE_MARKERS: tuple[str, ...] = (
    "unfortunately, bots use duckduckgo",
    "anomaly-detected",
    "anomaly",
    "captcha",
)

# Ad/redirect junk hints inside the raw href (checked before uddg resolution).
_AD_HINTS: tuple[str, ...] = ("y.js", "ad_provider", "ad_domain", "ad_script")

_INTERNAL_HOST = "duckduckgo.com"

# SERP roots that are directories/social profiles, never the company's own
# site. Ranking penalty only — a matching candidate is still shown.
_NON_OFFICIAL: frozenset[str] = frozenset(
    {
        "wikipedia.org",
        "linkedin.com",
        "facebook.com",
        "twitter.com",
        "x.com",
        "youtube.com",
        "crunchbase.com",
        "bloomberg.com",
        "glassdoor.com",
        "indeed.com",
        "reddit.com",
    }
)

MAX_RESULTS = 10


def _no_match() -> dict:
    return {"status": "no_match", "domain": None, "candidates": []}


def build_search_url(name: str) -> str:
    """Search URL for "<name> official website", URL-encoded (pure)."""
    return f"{DDG_HTML_URL}?q={quote_plus(f'{name} official website')}"


def _challenge_marker(html: str) -> str | None:
    """First challenge marker found in the body (case-insensitive), or None."""
    low = (html or "").casefold()
    for marker in _CHALLENGE_MARKERS:
        if marker in low:
            return marker
    return None


def _resolve_href(href: str) -> str | None:
    """Result anchor href -> real target URL (pure).

    DDG organic anchors are ``//duckduckgo.com/l/?uddg=<encoded>&rut=...``
    redirect wrappers (T1 probe); the uddg param carries the real target and
    is unquoted here (parse_qs decodes once, unquote again for the doubly
    encoded wrappers seen in the wild). Direct hrefs pass through.
    """
    value = (href or "").strip()
    if not value:
        return None
    if value.startswith("//"):
        value = "https:" + value
    elif value.startswith("/"):
        value = f"https://{_INTERNAL_HOST}{value}"
    if _INTERNAL_HOST + "/l/" in value.casefold():
        try:
            query = urlsplit(value).query
        except ValueError:
            return value
        target = (parse_qs(query).get("uddg") or [None])[0]
        if target:
            return unquote(target)
    return value


def _is_ad_href(href: str) -> bool:
    low = (href or "").casefold()
    return any(hint in low for hint in _AD_HINTS)


def _is_internal(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").casefold()
    except ValueError:
        return True
    return host == _INTERNAL_HOST or host.endswith("." + _INTERNAL_HOST)


def parse_results(html: str) -> list[dict]:
    """Organic SERP results as ranked candidates (pure).

    Up to MAX_RESULTS ``a.result__a`` anchors in document order; uddg
    wrappers are resolved to their real target; non-http(s), duckduckgo.com
    internal and ad links are skipped. ``score`` is a placeholder (0) here —
    pick_ddg_candidate owns scoring.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "lxml")
    candidates: list[dict] = []
    seen: set[str] = set()
    for anchor in soup.select("a.result__a"):
        if len(candidates) >= MAX_RESULTS:
            break
        href = anchor.get("href") or ""
        if _is_ad_href(href):
            continue
        url = _resolve_href(href)
        if not url:
            continue
        if not url.casefold().startswith(("http://", "https://")):
            continue
        if _is_internal(url):
            continue
        domain = root_domain(url)
        if not domain:
            continue
        if url in seen:
            continue
        seen.add(url)
        candidates.append(
            {
                "position": len(candidates) + 1,
                "title": anchor.get_text(" ", strip=True),
                "url": url,
                "domain": domain,
                "score": 0,
            }
        )
    return candidates


def _domain_tokens(domain: str | None) -> set[str]:
    """Registrable-domain label split into word tokens (pure)."""
    root = root_domain(domain)
    if not root:
        return set()
    for tld in sorted(MULTI_LABEL_TLDS, key=len, reverse=True):
        suffix = "." + tld
        if root.endswith(suffix):
            root = root[: -len(suffix)]
            break
    label = root.rsplit(".", 1)[0] if "." in root else root
    return {tok for tok in re.split(r"[^a-z0-9]+", label.casefold()) if tok}


def _score_candidate(candidate: dict, name: str) -> int:
    """Display ranking score (pure): +2 strict #1+name match, +2 two-way
    token overlap between name and domain, -1 known non-official root."""
    domain = candidate.get("domain") or ""
    score = 0
    if candidate.get("position") == 1 and domain and name_matches_domain(name, domain):
        score += 2
    name_toks = set(name_tokens(name))
    dom_toks = _domain_tokens(domain)
    if name_toks and dom_toks and bool(name_toks & dom_toks):
        score += 2
    if domain in _NON_OFFICIAL:
        score -= 1
    return score


def pick_ddg_candidate(candidates: list[dict], name: str) -> dict:
    """Score and decide from parsed SERP candidates (pure).

    Ranking is display-only. Decision: resolved ONLY when exactly one
    candidate carries the strict signal (#1 position AND name_matches_domain)
    AND its score strictly beats every other candidate (the overlap rule —
    a tie is never auto-picked). Ambiguous when plausible candidates exist
    but the strict signal is absent or split; no_match on zero candidates.
    """
    if not candidates:
        return _no_match()
    scored: list[dict] = []
    for candidate in candidates:
        item = dict(candidate)
        item["score"] = _score_candidate(candidate, name)
        scored.append(item)
    scored.sort(key=lambda c: (-c["score"], c.get("position") or 0))
    strict = [
        c
        for c in scored
        if c.get("position") == 1 and name_matches_domain(name, c.get("domain") or "")
    ]
    if len(strict) == 1:
        top = strict[0]
        if all(top["score"] > c["score"] for c in scored if c is not top):
            return {
                "status": "resolved",
                "domain": top["domain"],
                "candidates": scored,
            }
    return {"status": "ambiguous", "domain": None, "candidates": scored}


def _challenge_error(marker: str) -> dict:
    return {
        "status": "error",
        "domain": None,
        "candidates": [],
        "error": f"ddg challenge detected: {marker}",
    }


def _fetch_error(detail: str) -> dict:
    return {
        "status": "error",
        "domain": None,
        "candidates": [],
        "error": f"ddg fetch failed: {detail}",
    }


def _default_fetcher():
    """Chrome-impersonated fetcher per the T1 probe (curl_cffi, lazy-built).

    Transport goes through the module-level ``curl_cffi_get`` seam in
    src/core/curl_fetcher.py, which tests patch instead of hitting network.
    """
    from src.core.curl_fetcher import CurlCffiFetcher

    return CurlCffiFetcher(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
    )


class DdgSerpResolver:
    """Name -> company domain from the DDG HTML SERP (curl tier).

    ``fetcher`` is injectable for tests: a CurlCffiFetcher-like object
    (``.get(url)``) or a plain callable ``url -> response-or-str``
    (capterra_resolve style). Default: a lazily built chrome-impersonated
    CurlCffiFetcher whose transport is the ``curl_cffi_get`` patch seam.
    ``registry`` is accepted for house-style parity with the other identity
    resolvers; this module never writes to it — the waterfall wiring layer
    owns persistence and the 2-source agreement rule (never-guess contract).
    """

    def __init__(
        self,
        fetcher: Optional[Union[object, Callable[[str], object]]] = None,
        registry=None,
        clock=time.monotonic,
        sleep=time.sleep,
    ):
        self._fetcher = fetcher
        self._built_fetcher = None
        self.registry = registry
        self.clock = clock
        self.sleep = sleep
        self._last_request_at: float | None = None

    def discover(self, name: str) -> dict:
        """Name -> {"status", "domain", "candidates"}. Never raises.

        Challenge/blocked bodies and fetch failures surface as status
        "error" (marker/failure recorded) — the stage no-ops, it does not
        silently become a no_match.
        """
        try:
            return self._discover(name)
        except Exception as exc:  # network/parse failures never raise
            logger.warning("ddg_ids: discovery failed for {!r}: {}", name, exc)
            return _no_match()

    def resolve_all(self, names: list[str]) -> dict[str, dict]:
        """Batch entry with per-item try/except isolation (resolve_all style)."""
        out: dict[str, dict] = {}
        for name in names:
            try:
                out[name] = self.discover(name)
            except Exception as exc:  # one failing name never kills the pass
                logger.warning("ddg_ids: resolve failed for {!r}: {}", name, exc)
                out[name] = _no_match()
        return out

    def _pace(self) -> None:
        """Enforce the DDG_PACE_S minimum interval before a network call."""
        now = self.clock()
        if self._last_request_at is not None:
            remaining = DDG_PACE_S - (now - self._last_request_at)
            if remaining > 0:
                self.sleep(remaining)
                now += remaining  # sleep advances the monotonic clock
        self._last_request_at = now

    def _fetcher_instance(self):
        if self._fetcher is not None:
            return self._fetcher
        if self._built_fetcher is None:  # lazy: only when a call is needed
            self._built_fetcher = _default_fetcher()
        return self._built_fetcher

    def _fetch(self, url: str) -> tuple[Optional[str], int]:
        """One paced GET -> (body text or None, status). Tolerates fetcher
        objects with ``.get`` and plain callables (capterra_resolve style).

        The status is returned for diagnostics ONLY — DDG challenge pages
        ship with 200 and 202 alike, so callers validate the body.
        """
        self._pace()
        fetcher = self._fetcher_instance()
        resp = fetcher.get(url) if hasattr(fetcher, "get") else fetcher(url)
        body = getattr(resp, "body", resp)
        status = getattr(resp, "status", getattr(resp, "status_code", 200))
        if body is None:
            return None, int(status)
        if isinstance(body, bytes):
            return body.decode("utf-8", "replace"), int(status)
        return str(body), int(status)

    def _discover(self, name: str) -> dict:
        term = (name or "").strip()
        if not term:
            return _no_match()
        try:
            html, status = self._fetch(build_search_url(term))
        except Exception as exc:  # network failure — error, not no_match
            logger.warning("ddg_ids: fetch failed for {!r}: {}", name, exc)
            return _fetch_error(str(exc))
        if html is None:
            return _fetch_error(f"empty response (status {status})")
        marker = _challenge_marker(html)
        if marker is not None:
            logger.warning(
                "ddg_ids: challenge body for {!r} (marker {!r}, status {})",
                name,
                marker,
                status,
            )
            return _challenge_error(marker)
        return pick_ddg_candidate(parse_results(html), term)
