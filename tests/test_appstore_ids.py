"""Tests for App Store track ID resolution (fake fetcher, no network)."""

from __future__ import annotations

import json

from src.core.models import Account
from src.identity.appstore_ids import (
    AppStoreIdResolver,
    build_search_url,
    parse_search_results,
    pick_track_id,
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


def _response(results: list[dict], count: int | None = None) -> bytes:
    return json.dumps(
        {"resultCount": len(results) if count is None else count, "results": results}
    ).encode()


def test_single_match_fills_app_store_id_and_upserts():
    body = _response([{"trackId": 1232780281, "trackName": "Notion - Notes, Tasks, AI"}])
    fetcher = FakeFetcher(body)
    registry = FakeRegistry()
    acct = Account(domain="notion.so", name="Notion")
    out = AppStoreIdResolver(fetcher, registry).resolve_all([acct])
    assert out == {"notion.so": "1232780281"}
    assert acct.app_store_id == "1232780281"
    assert len(registry.upserts) == 1
    assert registry.upserts[0][0] is acct
    assert len(fetcher.tasks) == 1
    url = fetcher.tasks[0].url
    assert "term=Notion" in url
    assert "entity=software" in url
    assert "country=US" in url


def test_zero_results_returns_none_and_no_upsert():
    body = _response([])
    fetcher = FakeFetcher(body)
    registry = FakeRegistry()
    acct = Account(domain="ghosted.example", name="Ghosted")
    out = AppStoreIdResolver(fetcher, registry).resolve_all([acct])
    assert out == {"ghosted.example": None}
    assert acct.app_store_id is None
    assert registry.upserts == []


def test_ambiguous_without_name_winner_returns_none():
    body = _response(
        [
            {"trackId": 1, "trackName": "Acme Alpha"},
            {"trackId": 2, "trackName": "Acme Beta"},
        ]
    )
    fetcher = FakeFetcher(body)
    registry = FakeRegistry()
    acct = Account(domain="acme.example", name="Totally Unrelated")
    out = AppStoreIdResolver(fetcher, registry).resolve_all([acct])
    assert out == {"acme.example": None}
    assert acct.app_store_id is None
    assert registry.upserts == []


def test_ambiguous_with_clear_name_winner_picks_it():
    body = _response(
        [
            {"trackId": 1, "trackName": "Acme Alpha"},
            {"trackId": 2, "trackName": "Beta Works"},
        ]
    )
    fetcher = FakeFetcher(body)
    registry = FakeRegistry()
    acct = Account(domain="acme.example", name="Acme")
    out = AppStoreIdResolver(fetcher, registry).resolve_all([acct])
    assert out == {"acme.example": "1"}
    assert acct.app_store_id == "1"
    assert len(registry.upserts) == 1


def test_network_error_returns_none_no_exception():
    fetcher = FakeFetcher(error=ConnectionError("boom"))
    registry = FakeRegistry()
    acct = Account(domain="down.example", name="Down")
    out = AppStoreIdResolver(fetcher, registry).resolve_all([acct])
    assert out == {"down.example": None}
    assert acct.app_store_id is None
    assert registry.upserts == []


def test_account_with_existing_app_store_id_is_skipped():
    fetcher = FakeFetcher(body=_response([{"trackId": 999, "trackName": "Whatever"}]))
    registry = FakeRegistry()
    acct = Account(domain="known.example", name="Known", app_store_id="42")
    out = AppStoreIdResolver(fetcher, registry).resolve_all([acct])
    assert out == {"known.example": "42"}
    assert fetcher.tasks == []
    assert registry.upserts == []


def test_search_term_falls_back_to_domain_stem():
    resolver = AppStoreIdResolver(FakeFetcher(), FakeRegistry())
    assert resolver.search_term(Account(domain="notion.so")) == "notion"
    assert resolver.search_term(Account(domain="notion.so", name="Notion")) == "Notion"


def test_build_search_url_and_parse_roundtrip():
    url = build_search_url("notion", country="GB", limit=5)
    assert url.startswith("https://itunes.apple.com/search?")
    assert "term=notion" in url
    assert "entity=software" in url
    assert "country=GB" in url
    assert "limit=5" in url
    results = parse_search_results(
        _response([{"trackId": 1232780281, "trackName": "Notion"}])
    )
    assert results == [{"trackId": 1232780281, "trackName": "Notion"}]


def test_pick_track_id_never_guesses_without_name():
    results = [
        {"trackId": 1, "trackName": "Acme Alpha"},
        {"trackId": 2, "trackName": "Acme Beta"},
    ]
    assert pick_track_id(Account(domain="acme.example"), results) is None
