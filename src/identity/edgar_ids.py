"""Map accounts to SEC CIK identifiers. Parsers are pure."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from loguru import logger

from src.core.models import Account
from src.identity.names import name_similarity, normalize_name

if TYPE_CHECKING:
    from src.core.http import HttpFetcher
    from src.identity.registry import AccountRegistry

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"

_IDENTITY_KEYS = (
    "cik",
    "name",
    "tickers",
    "sic",
    "sicDescription",
    "stateOfIncorporation",
    "entityType",
    "website",
    "category",
    "fiscalYearEnd",
)


def pad_cik(value: str | int) -> str:
    return str(value).strip().zfill(10)


def parse_company_tickers(body: bytes) -> list[dict]:
    raw = json.loads(body)
    rows = []
    items = raw.values() if isinstance(raw, dict) else raw
    for item in items:
        rows.append(
            {
                "cik": pad_cik(item["cik_str"]),
                "ticker": item["ticker"],
                "name": item["title"],
            }
        )
    rows.sort(key=lambda r: r["cik"])
    return rows


def match_cik(
    account: Account,
    index: list[dict],
    *,
    min_similarity: float = 0.9,
) -> str | None:
    if account.ticker:
        want = account.ticker.casefold()
        hits = [r for r in index if r["ticker"].casefold() == want]
        if len(hits) == 1:
            return hits[0]["cik"]
        if len(hits) > 1:
            return None
    if account.name:
        want = normalize_name(account.name)
        exact = [r for r in index if normalize_name(r["name"]) == want]
        if len(exact) == 1:
            return exact[0]["cik"]
        if len(exact) > 1:
            return None
        fuzzy = [
            r
            for r in index
            if name_similarity(account.name, r["name"]) >= min_similarity
        ]
        domains = {r["cik"] for r in fuzzy}
        if len(domains) == 1:
            return fuzzy[0]["cik"]
    return None


def parse_submissions_identity(body: bytes) -> dict:
    raw = json.loads(body)
    out = {}
    for key in _IDENTITY_KEYS:
        if key not in raw or raw[key] in (None, "", []):
            continue
        val = raw[key]
        if key == "cik":
            val = pad_cik(val)
        out[key] = val
    return out


class EdgarIdentityResolver:
    def __init__(
        self,
        fetcher: "HttpFetcher",
        registry: "AccountRegistry",
        cache_ttl_hours: int = 168,
        sleep=time.sleep,
        retry_delay: float = 2.0,
    ):
        self.fetcher = fetcher
        self.registry = registry
        self.cache_ttl = timedelta(hours=cache_ttl_hours)
        self.sleep = sleep
        self.retry_delay = retry_delay
        self._index: list[dict] = []
        self._fetched_at: datetime | None = None

    def refresh_index(self) -> int:
        """Fetch the company_tickers index, retrying once on failure.

        A failed fetch degrades to an empty index (return 0) instead of
        raising, so one bad pass over the ~2MB file cannot zero the cohort
        resolve via an exception; the caller then just finds no CIKs.
        """
        task = _Task(source="sec_edgar", url=TICKERS_URL)
        for attempt in (1, 2):
            try:
                result = self.fetcher.get(task)
                if not result.ok or not result.doc or not result.doc.body:
                    raise ValueError(f"tickers index unavailable (ok={result.ok})")
                self._index = parse_company_tickers(result.doc.body)
                self._fetched_at = datetime.now(timezone.utc)
                return len(self._index)
            except Exception as exc:
                if attempt == 2:
                    logger.warning(
                        "edgar_ids: tickers index fetch failed after retry: {}", exc
                    )
                    return 0
                logger.warning(
                    "edgar_ids: tickers index fetch failed ({}); retrying once in {}s",
                    exc,
                    self.retry_delay,
                )
                self.sleep(self.retry_delay)
        return 0

    def resolve_all(self, accounts: list[Account]) -> dict[str, str | None]:
        if not self._index:
            self.refresh_index()
        out: dict[str, str | None] = {}
        for acct in accounts:
            try:
                cik = match_cik(acct, self._index)
                out[acct.domain] = cik
                if cik:
                    acct.cik = cik
                    self.registry.upsert(acct, source="sec_edgar")
            except Exception as exc:
                # Per-account isolation: one malformed row / failed write must
                # not kill the CIK pass for the rest of the cohort.
                logger.warning(
                    "edgar_ids: CIK resolution failed for {}: {}", acct.domain, exc
                )
                out[acct.domain] = None
        return out


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
