"""Map accounts to BBB (Better Business Bureau) profile URLs via the public
JSON search API.

Intended dispatch: wired into orchestrator.resolve() by the parent as a
`bbb`-flagged resolver alongside EdgarIdentityResolver (this module does not
self-dispatch).

The BBB search HTML page is JS-rendered (Next.js) — never parse it. The JSON
API behind it answers plain GETs:

    https://www.bbb.org/api/search?find_text=<term>&page=1&find_loc=<location>
    -> {"totalResults": N, "results": [{businessName, reportUrl, id, city, state, ...}]}

find_loc is effectively required (empty -> 0 results); "USA" is a safe
fallback, "City, ST" is precise. reportUrl is a PATH (/us/ca/...) — prepend
https://www.bbb.org. The resulting URL matches BbbProfileSource's documented
profile URL shape exactly (see src/sources/bbb/collector.py), so
extra_data["bbb_url"] feeds plan() directly.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urlencode

from loguru import logger

from src.core.models import Account

SEARCH_URL = "https://www.bbb.org/api/search"
_BBB_BASE = "https://www.bbb.org"
_EM_TAG_RE = re.compile(r"</?em>", re.IGNORECASE)

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


def build_location(account: Account) -> str:
    """Precise loc when the account has both city+region, else the USA fallback."""
    city = (account.hq_city or "").strip()
    region = (account.hq_region or "").strip()
    if city and region:
        return f"{city}, {region}"
    return "USA"


def build_search_url(term: str, location: str = "USA", page: int = 1) -> str:
    return f"{SEARCH_URL}?{urlencode({'find_text': term, 'page': page, 'find_loc': location})}"


def parse_search_results(body: bytes) -> list[dict]:
    raw = json.loads(body)
    results = raw.get("results") or []
    return [
        r
        for r in results
        if isinstance(r, dict) and r.get("reportUrl") is not None
    ]


def _clean_name(raw: str | None) -> str:
    """Strip BBB's <em> highlight tags before matching."""
    return _EM_TAG_RE.sub("", raw or "")


def pick_profile_url(account: Account, results: list[dict]) -> str | None:
    """Deterministic pick from search results. Never guesses on ambiguity."""
    if not results:
        return None
    if len(results) == 1:
        return _BBB_BASE + str(results[0]["reportUrl"])
    # Ambiguous (>1): accept only a clear name-substring winner, either direction.
    name = (account.name or "").casefold().strip()
    if not name:
        return None
    winners = []
    for r in results:
        bname = _clean_name(r.get("businessName")).casefold().strip()
        if not bname:
            continue
        if name in bname or bname in name:
            winners.append(r)
    if len(winners) == 1:
        return _BBB_BASE + str(winners[0]["reportUrl"])
    return None


class BbbProfileResolver:
    """Fills account.extra_data["bbb_url"] from the free, keyless BBB search API."""

    def __init__(self, fetcher, registry):
        self.fetcher = fetcher
        self.registry = registry

    def search_term(self, account: Account) -> str:
        if account.name:
            return account.name.strip()
        return (account.domain or "").split(".")[0]

    def resolve_all(self, accounts: list[Account]) -> dict[str, str | None]:
        out: dict[str, str | None] = {}
        for acct in accounts:
            existing = (acct.extra_data or {}).get("bbb_url")
            if existing:
                out[acct.domain] = existing
                continue
            url = self._search(acct)
            out[acct.domain] = url
            if url:
                data = dict(acct.extra_data or {})
                data["bbb_url"] = url
                acct.extra_data = data
                self.registry.upsert(acct, source="bbb_ids")
        return out

    def _search(self, account: Account) -> str | None:
        term = self.search_term(account)
        if not term:
            return None
        url = build_search_url(term, location=build_location(account), page=1)
        try:
            task = _Task(source="bbb_ids", url=url, domain=account.domain)
            result = self.fetcher.get(task)
            if not result.ok or not result.doc or not result.doc.body:
                return None
            results = parse_search_results(result.doc.body)
            return pick_profile_url(account, results)
        except Exception as exc:  # network/parse failures never raise
            logger.warning(
                "bbb_ids: search failed for {} (term={!r}): {}",
                account.domain,
                term,
                exc,
            )
            return None
