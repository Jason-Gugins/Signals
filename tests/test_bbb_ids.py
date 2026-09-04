"""Tests for BBB profile URL resolution (fake fetcher, no network)."""

from __future__ import annotations

import json

from src.core.models import Account
from src.identity.bbb_ids import (
    BbbProfileResolver,
    build_location,
    build_search_url,
    parse_search_results,
    pick_profile_url,
)


class FakeDoc:
    def __init__(self, body: bytes):
        self.body = body


class FakeResult:
    def __init__(self, body: bytes | None, ok: bool = True):
        self.ok = ok
        self.doc = FakeDoc(body) if body is not None else None


class FakeFetcher:
    def __init__(self, body: bytes | None = None, ok: bool = True, error: Exception | None = None):
        self.body = body
        self.ok = ok
        self.error = error
        self.tasks: list = []

    def get(self, task):
        if self.error is not None:
            raise self.error
        self.tasks.append(task)
        return FakeResult(self.body, ok=self.ok)


class FakeRegistry:
    def __init__(self):
        self.upserts: list[tuple[Account, str | None]] = []

    def upsert(self, account: Account, *, source: str | None = None):
        self.upserts.append((account, source))
        return account


def _response(results: list[dict], total: int | None = None) -> bytes:
    return json.dumps(
        {"totalResults": len(results) if total is None else total, "results": results}
    ).encode()


def test_single_match_fills_bbb_url_and_upserts():
    body = _response(
        [
            {
                "businessName": "Notion",
                "reportUrl": "/us/ca/san-francisco/profile/notion/11-9-900-12345",
                "id": "12345",
                "city": "San Francisco",
                "state": "CA",
            }
        ]
    )
    fetcher = FakeFetcher(body)
    registry = FakeRegistry()
    acct = Account(domain="notion.so", name="Notion")
    out = BbbProfileResolver(fetcher, registry).resolve_all([acct])
    expected = "https://www.bbb.org/us/ca/san-francisco/profile/notion/11-9-900-12345"
    assert out == {"notion.so": expected}
    assert acct.extra_data["bbb_url"] == expected
    assert len(registry.upserts) == 1
    assert registry.upserts[0][0] is acct
    assert registry.upserts[0][1] == "bbb_ids"
    assert len(fetcher.tasks) == 1


def test_multiple_results_with_clear_casefold_winner_after_em_strip():
    body = _response(
        [
            {
                "businessName": "<em>Notion</em> Labs",
                "reportUrl": "/us/ca/san-francisco/profile/notion-labs/11-1",
            },
            {
                "businessName": "Unrelated Co",
                "reportUrl": "/us/ny/profile/unrelated/22-2",
            },
        ]
    )
    fetcher = FakeFetcher(body)
    registry = FakeRegistry()
    acct = Account(domain="notion.so", name="Notion")
    out = BbbProfileResolver(fetcher, registry).resolve_all([acct])
    assert out == {"notion.so": "https://www.bbb.org/us/ca/san-francisco/profile/notion-labs/11-1"}
    assert len(registry.upserts) == 1


def test_multiple_results_no_clear_winner_returns_none():
    body = _response(
        [
            {"businessName": "Acme Alpha", "reportUrl": "/profile/a/1"},
            {"businessName": "Acme Beta", "reportUrl": "/profile/b/2"},
        ]
    )
    fetcher = FakeFetcher(body)
    registry = FakeRegistry()
    acct = Account(domain="acme.example", name="Totally Unrelated")
    out = BbbProfileResolver(fetcher, registry).resolve_all([acct])
    assert out == {"acme.example": None}
    assert acct.extra_data.get("bbb_url") is None
    assert registry.upserts == []


def test_zero_results_returns_none_and_no_upsert():
    body = _response([], total=0)
    fetcher = FakeFetcher(body)
    registry = FakeRegistry()
    acct = Account(domain="ghosted.example", name="Ghosted")
    out = BbbProfileResolver(fetcher, registry).resolve_all([acct])
    assert out == {"ghosted.example": None}
    assert registry.upserts == []


def test_network_error_returns_none_no_exception():
    fetcher = FakeFetcher(error=ConnectionError("boom"))
    registry = FakeRegistry()
    acct = Account(domain="down.example", name="Down")
    out = BbbProfileResolver(fetcher, registry).resolve_all([acct])
    assert out == {"down.example": None}
    assert registry.upserts == []


def test_account_with_existing_bbb_url_is_skipped():
    fetcher = FakeFetcher(body=_response([{"businessName": "Whatever", "reportUrl": "/x"}]))
    registry = FakeRegistry()
    acct = Account(
        domain="known.example", name="Known", extra_data={"bbb_url": "https://www.bbb.org/x"}
    )
    out = BbbProfileResolver(fetcher, registry).resolve_all([acct])
    assert out == {"known.example": "https://www.bbb.org/x"}
    assert fetcher.tasks == []
    assert registry.upserts == []


def test_build_location_uses_city_region_else_usa():
    def acct(**kw):
        return Account(domain="a.example", name="A", **kw)

    assert build_location(acct(hq_city="San Francisco", hq_region="CA")) == "San Francisco, CA"
    assert build_location(acct(hq_city="San Francisco")) == "USA"
    assert build_location(acct(hq_region="CA")) == "USA"
    assert build_location(acct()) == "USA"


def test_build_search_url_shape():
    url = build_search_url("Notion", location="San Francisco, CA")
    assert url.startswith("https://www.bbb.org/api/search?")
    assert "find_text=Notion" in url
    assert "page=1" in url
    assert "find_loc=San+Francisco%2C+CA" in url


def test_parse_search_results_filters_non_dicts():
    body = _response(
        [
            {"businessName": "Notion", "reportUrl": "/p/1"},
            "junk",
            {"no_report_url": True},
        ]
    )
    results = parse_search_results(body)
    assert results == [{"businessName": "Notion", "reportUrl": "/p/1"}]


def test_pick_profile_url_never_guesses_without_name():
    results = [
        {"businessName": "Acme Alpha", "reportUrl": "/a"},
        {"businessName": "Acme Beta", "reportUrl": "/b"},
    ]
    assert pick_profile_url(Account(domain="acme.example"), results) is None


def test_pick_profile_url_single_result_with_empty_name_accepted():
    results = [{"businessName": "Notion", "reportUrl": "/us/ca/profile/notion/1"}]
    acct = Account(domain="notion.so")
    url = pick_profile_url(acct, results)
    assert url == "https://www.bbb.org/us/ca/profile/notion/1"
