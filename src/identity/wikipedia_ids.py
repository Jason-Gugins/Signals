"""Discover company domains from Wikipedia external links.

Waterfall stage 2 of the Clay "find the company" clone (plan T3a):
list=search -> prop=extlinks on the top hit. Intended dispatch: wired into
the resolve waterfall by the parent as a keyless name->domain resolver
(this module does not self-dispatch). Exactly 2 GETs per name.

T1 probe: extlinks include blog deep links and the host's own links —
every extlink goes through root_domain() (which also rejects IPs, public
email hosts and other junk) plus a Wikimedia-family/archive drop-list
before scoring. One distinct surviving domain resolves; multiple come back
as ranked candidates for the review queue (never-guess contract).

Parsers are pure; the resolver is network-backed via the injected fetcher.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urlencode

from loguru import logger

from src.identity.domains import root_domain
from src.identity.names import name_matches_domain, normalize_name

API_URL = "https://en.wikipedia.org/w/api.php"

# Wikimedia-family + web-archive roots are never company domains. T1 probe
# found web.archive.org deep links among extlinks; wikipedia.org /
# wikimedia.org are the host's own outbound links.
NON_COMPANY_DOMAINS: frozenset[str] = frozenset(
    {
        "wikipedia.org",
        "wikimedia.org",
        "wikidata.org",
        "archive.org",
    }
)


# House style for fetch tasks outside src/sources/: local frozen-ish dataclass
# mirroring appstore_ids._Task.
@dataclass
class _Task:
    source: str
    url: str
    domain: str | None = None
    method: str = "GET"
    headers: dict | None = None
    json_body: dict | None = None

    def __post_init__(self) -> None:
        if self.headers is None:
            self.headers = {}


def build_search_url(name: str, limit: int = 3) -> str:
    return f"{API_URL}?{urlencode({'action': 'query', 'list': 'search', 'srsearch': name, 'format': 'json', 'srlimit': limit})}"


def build_extlinks_url(title: str, limit: int = 50) -> str:
    return f"{API_URL}?{urlencode({'action': 'query', 'titles': title, 'prop': 'extlinks', 'ellimit': limit, 'format': 'json'})}"


def parse_search(payload: bytes) -> list[dict]:
    """Search hits from a list=search payload (pure)."""
    raw = json.loads(payload)
    query = raw.get("query") or {}
    results = query.get("search") or []
    return [r for r in results if isinstance(r, dict) and r.get("title")]


def parse_extlinks(payload: bytes) -> list[str]:
    """External-link URL strings from a prop=extlinks payload (pure)."""
    raw = json.loads(payload)
    query = raw.get("query") or {}
    pages = query.get("pages") or {}
    urls: list[str] = []
    if not isinstance(pages, dict):
        return urls
    for page in pages.values():
        if not isinstance(page, dict):
            continue
        for link in page.get("extlinks") or []:
            if not isinstance(link, dict):
                continue
            value = link.get("*")
            if isinstance(value, str) and value.strip():
                urls.append(value.strip())
    return urls


def _no_match() -> dict:
    return {"status": "no_match", "domain": None, "candidates": []}


def _score_domain(title: str, name: str, domain: str) -> int:
    score = 0
    if name_matches_domain(name, domain):
        score += 2
    norm = normalize_name(name)
    if norm and norm in title.casefold():
        score += 1
    return score


def pick_wikipedia_domain(title: str, extlinks: list[str], name: str) -> dict:
    """Root-domain filter, score and decide from raw extlinks (pure).

    Every extlink is reduced to its root domain (drops URL-only junk and
    blog deep links collapse onto the apex) and Wikimedia-family/archive
    roots are discarded. Decision over distinct surviving domains:
    zero -> no_match, exactly one -> resolved, more than one -> ambiguous
    with ALL candidates ranked by score (never auto-picks the top one).
    """
    candidates: list[dict] = []
    seen: set[str] = set()
    for url in extlinks:
        domain = root_domain(url)
        if not domain or domain in NON_COMPANY_DOMAINS or domain in seen:
            continue
        seen.add(domain)
        candidates.append(
            {
                "title": title,
                "domain": domain,
                "url": url,
                "score": _score_domain(title, name, domain),
            }
        )
    if not candidates:
        return _no_match()
    candidates.sort(key=lambda c: c["score"], reverse=True)
    if len(candidates) == 1:
        return {
            "status": "resolved",
            "domain": candidates[0]["domain"],
            "candidates": candidates,
        }
    return {"status": "ambiguous", "domain": None, "candidates": candidates}


class WikipediaExtlinksResolver:
    """Name -> company domain from keyless Wikipedia extlinks (HttpFetcher tier).

    `registry` is accepted for house-style parity with the other identity
    resolvers; this module never writes to it — the waterfall wiring layer
    owns persistence and the 2-source agreement rule (never-guess contract).
    """

    def __init__(self, fetcher, registry):
        self.fetcher = fetcher
        self.registry = registry

    def discover(self, name: str) -> dict:
        """Name -> {"status", "domain", "candidates"}. Never raises."""
        try:
            return self._discover(name)
        except Exception as exc:  # network/parse failures never raise
            logger.warning("wikipedia_ids: discovery failed for {!r}: {}", name, exc)
            return _no_match()

    def resolve_all(self, names: list[str]) -> dict[str, dict]:
        """Batch entry with per-item try/except isolation (resolve_all style)."""
        out: dict[str, dict] = {}
        for name in names:
            try:
                out[name] = self.discover(name)
            except Exception as exc:  # one failing name never kills the pass
                logger.warning("wikipedia_ids: resolve failed for {!r}: {}", name, exc)
                out[name] = _no_match()
        return out

    def _discover(self, name: str) -> dict:
        term = (name or "").strip()
        if not term:
            return _no_match()
        search_result = self.fetcher.get(
            _Task(source="wikipedia", url=build_search_url(term))
        )
        if not search_result.ok or not search_result.doc or not search_result.doc.body:
            return _no_match()
        hits = parse_search(search_result.doc.body)
        if not hits:
            return _no_match()
        title = str(hits[0].get("title") or "").strip()
        if not title:
            return _no_match()
        ext_result = self.fetcher.get(
            _Task(source="wikipedia", url=build_extlinks_url(title))
        )
        if not ext_result.ok or not ext_result.doc or not ext_result.doc.body:
            return _no_match()
        return pick_wikipedia_domain(title, parse_extlinks(ext_result.doc.body), term)
