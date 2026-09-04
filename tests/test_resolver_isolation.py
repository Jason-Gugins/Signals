"""Per-account isolation in resolver passes + EDGAR index retry (P2 Task 8).

The orchestrator wraps each resolver's whole account loop in one try/except
(last-resort guard), so before this fix a single failing account (malformed
row, registry write failure) zeroed the CIK/appstore/bbb pass for the entire
cohort, and one failed fetch of the ~2MB company_tickers.json zeroed it too.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.core.models import Account
from src.identity import edgar_ids
from src.identity.appstore_ids import AppStoreIdResolver
from src.identity.bbb_ids import BbbProfileResolver
from src.identity.edgar_ids import EdgarIdentityResolver

FIXTURE = Path(__file__).parent / "fixtures" / "sec" / "company_tickers.json"


class FakeDoc:
    def __init__(self, body: bytes):
        self.body = body


class FakeResult:
    def __init__(self, body: bytes | None, ok: bool = True):
        self.ok = ok
        self.doc = FakeDoc(body) if body is not None else None


class FakeFetcher:
    def __init__(self, body: bytes | None = None, ok: bool = True):
        self.body = body
        self.ok = ok
        self.tasks: list = []

    def get(self, task):
        self.tasks.append(task)
        return FakeResult(self.body, ok=self.ok)


class FakeRegistry:
    def __init__(self, fail_domain: str | None = None):
        self.upserts: list[tuple[Account, str | None]] = []
        self.fail_domain = fail_domain

    def upsert(self, account: Account, *, source: str | None = None):
        if self.fail_domain and account.domain == self.fail_domain:
            raise ValueError(f"registry write failed for {account.domain}")
        self.upserts.append((account, source))
        return account


class SleepRecorder:
    def __init__(self):
        self.sleeps: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def _appstore_body(track_id: int = 1232780281) -> bytes:
    return json.dumps(
        {"resultCount": 1, "results": [{"trackId": track_id, "trackName": "Notion"}]}
    ).encode()


def _bbb_body() -> bytes:
    return json.dumps(
        {
            "totalResults": 1,
            "results": [
                {"businessName": "Notion", "reportUrl": "/us/ca/profile/notion/1"}
            ],
        }
    ).encode()


# --- per-account isolation --------------------------------------------------


def test_edgar_resolver_isolates_failing_account(monkeypatch):
    fetcher = FakeFetcher(FIXTURE.read_bytes())
    registry = FakeRegistry()
    resolver = EdgarIdentityResolver(fetcher, registry)
    real_match_cik = edgar_ids.match_cik

    def flaky_match_cik(acct, index, **kw):
        if acct.domain == "bad.example":
            raise ValueError("malformed account row")
        return real_match_cik(acct, index, **kw)

    monkeypatch.setattr(edgar_ids, "match_cik", flaky_match_cik)
    accounts = [
        Account(domain="apple.com", name="Apple Inc.", ticker="AAPL"),
        Account(domain="bad.example", name="Bad"),
        Account(domain="msft.com", name="Microsoft", ticker="MSFT"),
    ]
    out = resolver.resolve_all(accounts)
    # the healthy accounts still resolved
    assert out["apple.com"] == "0000320193"
    assert out["msft.com"] == "0000789019"
    # the failing one is skipped as None, not poisoning the pass
    assert out["bad.example"] is None
    assert [u[0].domain for u in registry.upserts] == ["apple.com", "msft.com"]


def test_appstore_resolver_isolates_failing_account():
    # registry write fails for exactly ONE account (the unprotected part of the
    # loop body outside _search's internal guard)
    fetcher = FakeFetcher(_appstore_body())
    registry = FakeRegistry(fail_domain="bad.example")
    resolver = AppStoreIdResolver(fetcher, registry)
    accounts = [
        Account(domain="notion.so", name="Notion"),
        Account(domain="bad.example", name="Bad"),
        Account(domain="other.example", name="Notion Two"),
    ]
    out = resolver.resolve_all(accounts)
    assert out["notion.so"] == "1232780281"
    assert out["bad.example"] is None
    assert out["other.example"] == "1232780281"
    assert [u[0].domain for u in registry.upserts] == ["notion.so", "other.example"]


def test_bbb_resolver_isolates_failing_account():
    fetcher = FakeFetcher(_bbb_body())
    registry = FakeRegistry(fail_domain="bad.example")
    resolver = BbbProfileResolver(fetcher, registry)
    accounts = [
        Account(domain="notion.so", name="Notion"),
        Account(domain="bad.example", name="Bad"),
        Account(domain="other.example", name="Notion Two"),
    ]
    out = resolver.resolve_all(accounts)
    expected = "https://www.bbb.org/us/ca/profile/notion/1"
    assert out["notion.so"] == expected
    assert out["bad.example"] is None
    assert out["other.example"] == expected
    assert [u[0].domain for u in registry.upserts] == ["notion.so", "other.example"]


# --- EDGAR index retry -------------------------------------------------------


class FlakyIndexFetcher:
    """First get raises (transport failure), then serves the tickers fixture."""

    def __init__(self, body: bytes):
        self.body = body
        self.calls = 0

    def get(self, task):
        self.calls += 1
        if self.calls == 1:
            raise ConnectionError("transient transport failure")
        return FakeResult(self.body)


class AlwaysDownFetcher:
    def __init__(self):
        self.calls = 0

    def get(self, task):
        self.calls += 1
        raise ConnectionError("down")


def test_edgar_refresh_index_retries_once_then_resolves():
    fetcher = FlakyIndexFetcher(FIXTURE.read_bytes())
    registry = FakeRegistry()
    sleep_rec = SleepRecorder()
    resolver = EdgarIdentityResolver(fetcher, registry, sleep=sleep_rec, retry_delay=2.5)
    acct = Account(domain="apple.com", name="Apple Inc.", ticker="AAPL")
    out = resolver.resolve_all([acct])
    assert out == {"apple.com": "0000320193"}
    assert fetcher.calls == 2  # first attempt failed, retry succeeded
    assert sleep_rec.sleeps == [2.5]  # injectable sleep, no real waiting


def test_edgar_refresh_index_gives_up_after_one_retry():
    fetcher = AlwaysDownFetcher()
    registry = FakeRegistry()
    sleep_rec = SleepRecorder()
    resolver = EdgarIdentityResolver(fetcher, registry, sleep=sleep_rec, retry_delay=1.0)
    acct = Account(domain="apple.com", name="Apple Inc.", ticker="AAPL")
    out = resolver.resolve_all([acct])
    # pass degrades to "no CIK found" instead of raising
    assert out == {"apple.com": None}
    assert resolver._index == []
    assert fetcher.calls == 2  # exactly one retry
    assert sleep_rec.sleeps == [1.0]
