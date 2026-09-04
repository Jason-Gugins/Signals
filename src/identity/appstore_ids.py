"""Map accounts to Apple App Store track IDs via the iTunes Search API.

Intended dispatch: wired into orchestrator.resolve() by the parent as an
`appstore`-flagged resolver alongside EdgarIdentityResolver (this module does
not self-dispatch).

Parsers are pure; the resolver is network-backed via the injected fetcher.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urlencode

from loguru import logger

from src.core.models import Account

SEARCH_URL = "https://itunes.apple.com/search"

# House style for fetch tasks outside src/sources/: local frozen-ish dataclass
# mirroring edgar_ids._Task.
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


def build_search_url(term: str, country: str = "US", limit: int = 5) -> str:
    return f"{SEARCH_URL}?{urlencode({'term': term, 'entity': 'software', 'country': country, 'limit': limit})}"


def parse_search_results(body: bytes) -> list[dict]:
    raw = json.loads(body)
    results = raw.get("results") or []
    return [r for r in results if isinstance(r, dict) and r.get("trackId") is not None]


def pick_track_id(account: Account, results: list[dict]) -> str | None:
    """Deterministic pick from search results. Never guesses on ambiguity."""
    if not results:
        return None
    if len(results) == 1:
        return str(results[0]["trackId"])
    # Ambiguous (>1): accept only a clear name-substring winner, either direction.
    name = (account.name or "").casefold().strip()
    if not name:
        return None
    winners = []
    for r in results:
        track = str(r.get("trackName") or "").casefold()
        if not track:
            continue
        if name in track or track in name:
            winners.append(r)
    if len(winners) == 1:
        return str(winners[0]["trackId"])
    return None


class AppStoreIdResolver:
    """Fills account.app_store_id from the free, keyless iTunes Search API."""

    def __init__(self, fetcher, registry, country: str = "US"):
        self.fetcher = fetcher
        self.registry = registry
        self.country = country

    def search_term(self, account: Account) -> str:
        if account.name:
            return account.name.strip()
        return (account.domain or "").split(".")[0]

    def resolve_all(self, accounts: list[Account]) -> dict[str, str | None]:
        out: dict[str, str | None] = {}
        for acct in accounts:
            if acct.app_store_id:
                out[acct.domain] = acct.app_store_id
                continue
            track_id = self._search(acct)
            out[acct.domain] = track_id
            if track_id:
                acct.app_store_id = track_id
                self.registry.upsert(acct, source="appstore")
        return out

    def _search(self, account: Account) -> str | None:
        term = self.search_term(account)
        if not term:
            return None
        url = build_search_url(term, country=self.country, limit=5)
        try:
            task = _Task(source="appstore", url=url, domain=account.domain)
            result = self.fetcher.get(task)
            if not result.ok or not result.doc or not result.doc.body:
                return None
            results = parse_search_results(result.doc.body)
            return pick_track_id(account, results)
        except Exception as exc:  # network/parse failures never raise
            logger.warning(
                "appstore_ids: search failed for {} (term={!r}): {}",
                account.domain,
                term,
                exc,
            )
            return None
