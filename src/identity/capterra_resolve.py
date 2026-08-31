"""Auto-resolve a Capterra product segment (<numeric-id>/<Slug>) from a company name.

Mirrors ``src/identity/g2_resolve.py`` but targets the Capterra search endpoint
(see ``data/probe/CAPTERRA_SEARCH_FINDINGS.md``):

    GET https://www.capterra.com/search/?query=<name>

Server-rendered HTML; product cards are ``div[data-testid='search-product-card']``
containing ``a[data-testid='thumbnail-link']`` anchors with
``href="/p/<id>/<Slug>/"``. Candidates come from card anchors ONLY — the embedded
RSC payload also contains ``/p/<id>/<Slug>`` segments for non-card products
(family pollution, e.g. ``10039800/Jira-Backup-and-Restore``), so the
whole-body regex must not be used for selection.

Selection ladder: exact name → normalized name → unique substring match →
ambiguous list. We never silently fall back to the first result (the spike's
guidance: do not guess between plausible matches like JIRA vs JIRA Service
Management).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional, Union
from urllib.parse import quote_plus

CAPTERNA_SEARCH_URL = "https://www.capterra.com/search/?query={query}"

# Matches /p/<numeric-id>/<Slug> (canonical Capterra product segment).
_P_SEGMENT = re.compile(r"/p/(\d+)/([A-Za-z0-9-]+)")


@dataclass
class ResolveResult:
    """Outcome of a Capterra name → segment resolution."""

    status: str  # "resolved" | "ambiguous" | "no_match" | "error"
    segment: Optional[str] = None  # "<id>/<Slug>" when resolved
    candidates: list[dict] = field(default_factory=list)
    url: Optional[str] = None


def fetch_capterra_search_url(name: str) -> str:
    """Build the Capterra search URL for a company/product name. PURE."""
    return CAPTERNA_SEARCH_URL.format(query=quote_plus(name))


def parse_capterra_search_results(html: str) -> list[dict]:
    """Parse Capterra search HTML into [{segment, id, slug, name, url}]. PURE.

    Only anchors inside ``search-product-card`` cards are considered
    (``thumbnail-link`` anchors), so add-ons beyond the visible cards in the
    RSC payload never pollute the candidate list.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    results: list[dict] = []
    seen: set[str] = set()
    for card in soup.select("div[data-testid='search-product-card']"):
        anchor = card.select_one("a[data-testid='thumbnail-link']") or card.select_one("a[href*='/p/']")
        if anchor is None:
            continue
        href = anchor.get("href", "") or ""
        m = _P_SEGMENT.search(href)
        if not m:
            continue
        segment = f"{m.group(1)}/{m.group(2)}"
        if segment in seen:
            continue
        name_el = card.select_one("a[data-testid='product-name']") or card.select_one("[data-testid='product-name']")
        name = name_el.get_text(strip=True) if name_el else m.group(2).replace("-", " ")
        url = href if href.startswith("http") else f"https://www.capterra.com{href}"
        seen.add(segment)
        results.append({"segment": segment, "id": m.group(1), "slug": m.group(2), "name": name, "url": url})
    return results


def _norm(value: str) -> str:
    """Lowercase and strip everything except alphanumerics. PURE."""
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def resolve_capterra_from_results(name: str, results: list[dict]) -> ResolveResult:
    """Pick the Capterra segment from parsed search results. PURE.

    Ladder: 1) exact case-insensitive name, 2) normalized (punctuation/space
    insensitive) equality, 3) unique substring match, else ambiguous list /
    no match. Never guesses among multiple strong candidates.
    """
    if not results:
        return ResolveResult(status="no_match")
    target = name.lower().strip()
    target_norm = _norm(name)

    for r in results:
        if r["name"].lower().strip() == target:
            return ResolveResult(status="resolved", segment=r["segment"], candidates=[r])
    for r in results:
        if _norm(r["name"]) == target_norm:
            return ResolveResult(status="resolved", segment=r["segment"], candidates=[r])

    partial = [r for r in results if target_norm and target_norm in _norm(r["name"])]
    if len(partial) == 1:
        return ResolveResult(status="resolved", segment=partial[0]["segment"], candidates=[partial[0]])
    if len(partial) > 1:
        return ResolveResult(status="ambiguous", candidates=partial)
    return ResolveResult(status="no_match", candidates=results)


def _default_fetcher():
    """Chrome-impersonated fetcher per the Task-1 spike (curl_cffi)."""
    from src.core.curl_fetcher import CurlCffiFetcher

    return CurlCffiFetcher(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")


def _fetch_html(url: str, fetcher) -> Optional[str]:
    """Fetch a URL and return the response body as text, or None. Tolerates
    fetcher objects with ``.get`` and plain callables."""
    resp = fetcher.get(url) if hasattr(fetcher, "get") else fetcher(url)
    body = getattr(resp, "body", resp)
    status = getattr(resp, "status", getattr(resp, "status_code", 200))
    if status != 200 or not body:
        return None
    if isinstance(body, bytes):
        return body.decode("utf-8", "replace")
    return str(body)


def resolve_capterra(
    name: str,
    fetcher: Optional[Union[object, Callable[[str], str]]] = None,
) -> ResolveResult:
    """Resolve a company/product name to a Capterra ``<id>/<Slug>`` segment.

    ``fetcher`` may be a CurlCffiFetcher-like object (``.get(url)``) or a
    callable ``url -> html``; defaults to the chrome-impersonated curl_cffi
    fetcher verified in the Task-1 spike.
    """
    url = fetch_capterra_search_url(name)
    if fetcher is None:
        fetcher = _default_fetcher()
    try:
        html = _fetch_html(url, fetcher)
    except Exception as exc:  # network/anti-bot failure — caller decides
        return ResolveResult(status="error", url=url, candidates=[{"error": str(exc)}])
    if html is None:
        return ResolveResult(status="error", url=url)
    results = parse_capterra_search_results(html)
    result = resolve_capterra_from_results(name, results)
    result.url = url
    return result
