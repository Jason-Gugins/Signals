"""Tests for Wikipedia extlinks domain discovery (fake fetcher, no network)."""

from __future__ import annotations

import json

from src.identity.wikipedia_ids import (
    WikipediaExtlinksResolver,
    build_extlinks_url,
    build_search_url,
    parse_extlinks,
    parse_search,
    pick_wikipedia_domain,
)


class FakeDoc:
    def __init__(self, body: bytes):
        self.body = body


class FakeResult:
    def __init__(self, body: bytes | None, ok: bool = True):
        self.ok = ok
        self.doc = FakeDoc(body) if body is not None else None


class FakeFetcher:
    """Routes canned T1-probe-shaped payloads by URL substring (no network)."""

    def __init__(
        self,
        routes: dict[str, bytes] | None = None,
        ok: bool = True,
        error: Exception | None = None,
    ):
        self.routes = routes or {}
        self.ok = ok
        self.error = error
        self.tasks: list = []

    def get(self, task):
        if self.error is not None:
            raise self.error
        self.tasks.append(task)
        for key, body in self.routes.items():
            if key in task.url:
                return FakeResult(body, ok=self.ok)
        return FakeResult(None, ok=False)


class FakeRegistry:
    def __init__(self):
        self.upserts: list = []

    def upsert(self, account, *, source=None):
        self.upserts.append((account, source))
        return account


def _search_payload(titles: list[str]) -> bytes:
    return json.dumps(
        {
            "batchcomplete": "",
            "query": {
                "searchinfo": {"totalhits": len(titles)},
                "search": [{"ns": 0, "title": t, "pageid": i} for i, t in enumerate(titles, 1)],
            },
        }
    ).encode()


def _extlinks_payload(title: str, urls: list[str]) -> bytes:
    return json.dumps(
        {
            "query": {
                "pages": {
                    "32845520": {
                        "pageid": 32845520,
                        "ns": 0,
                        "title": title,
                        "extlinks": [{"*": u} for u in urls],
                    }
                }
            }
        }
    ).encode()


# Real T1 probe extlink subset: apex first, then blog deep links.
_T1_TITLE = "Stripe, Inc."
_T1_EXTLINKS = [
    "https://stripe.com/",
    "https://stripe.com/blog/atlas-llc",
    "https://stripe.com/blog/terminal",
    "https://stripe.com/blog/terminal-in-person-payments",
]


def _stripe_routes(extlinks: list[str]) -> dict[str, bytes]:
    return {
        "list=search": _search_payload([_T1_TITLE, "OpenRouter", "Frontier Climate"]),
        "prop=extlinks": _extlinks_payload(_T1_TITLE, extlinks),
    }


def test_resolved_single_distinct_domain_from_blog_deeplinks():
    fetcher = FakeFetcher(_stripe_routes(_T1_EXTLINKS))
    registry = FakeRegistry()
    out = WikipediaExtlinksResolver(fetcher, registry).discover("Stripe, Inc.")
    assert out["status"] == "resolved"
    assert out["domain"] == "stripe.com"
    # Blog deep links collapse onto the apex: exactly one candidate.
    assert len(out["candidates"]) == 1
    cand = out["candidates"][0]
    assert cand["title"] == _T1_TITLE
    assert cand["url"] == "https://stripe.com/"
    assert cand["score"] == 3
    # Exactly 2 GETs. Never auto-persists.
    assert len(fetcher.tasks) == 2
    assert "list=search" in fetcher.tasks[0].url
    assert "prop=extlinks" in fetcher.tasks[1].url
    assert registry.upserts == []


def test_ambiguous_two_distinct_domains_ranks_but_never_picks():
    fetcher = FakeFetcher(
        _stripe_routes(
            [
                "https://stripe.com/",
                "https://techcrunch.com/2011/03/28/stealth-payment-startup-stripe-paypal/",
            ]
        )
    )
    out = WikipediaExtlinksResolver(fetcher, FakeRegistry()).discover("Stripe, Inc.")
    assert out["status"] == "ambiguous"
    assert out["domain"] is None
    assert [c["domain"] for c in out["candidates"]] == ["stripe.com", "techcrunch.com"]
    assert [c["score"] for c in out["candidates"]] == [3, 1]


def test_no_extlinks_returns_no_match():
    fetcher = FakeFetcher(_stripe_routes([]))
    out = WikipediaExtlinksResolver(fetcher, FakeRegistry()).discover("Stripe, Inc.")
    assert out == {"status": "no_match", "domain": None, "candidates": []}


def test_wikipedia_and_archive_junk_extlinks_dropped():
    fetcher = FakeFetcher(
        _stripe_routes(
            [
                "https://en.wikipedia.org/wiki/Stripe,_Inc.",
                "https://web.archive.org/web/2024/https://stripe.com/",
                "https://www.wikimedia.org/",
                "https://stripe.com/",
            ]
        )
    )
    out = WikipediaExtlinksResolver(fetcher, FakeRegistry()).discover("Stripe, Inc.")
    assert out["status"] == "resolved"
    assert out["domain"] == "stripe.com"
    assert [c["domain"] for c in out["candidates"]] == ["stripe.com"]

    # All-junk extlinks leave nothing: no_match.
    junk_only = FakeFetcher(
        _stripe_routes(
            [
                "https://en.wikipedia.org/wiki/Stripe,_Inc.",
                "https://web.archive.org/web/2024/https://stripe.com/",
            ]
        )
    )
    out = WikipediaExtlinksResolver(junk_only, FakeRegistry()).discover("Stripe, Inc.")
    assert out == {"status": "no_match", "domain": None, "candidates": []}


def test_search_url_encodes_name():
    url = build_search_url("Acme & Co")
    assert url.startswith("https://en.wikipedia.org/w/api.php?")
    assert "action=query" in url
    assert "list=search" in url
    assert "srsearch=Acme+%26+Co" in url
    assert "format=json" in url
    assert "srlimit=3" in url
    ext_url = build_extlinks_url("Stripe, Inc.")
    assert "titles=Stripe%2C+Inc." in ext_url
    assert "prop=extlinks" in ext_url
    assert "ellimit=50" in ext_url


def test_parse_search_roundtrip():
    hits = parse_search(_search_payload(["Stripe, Inc.", "OpenRouter"]))
    assert [h["title"] for h in hits] == ["Stripe, Inc.", "OpenRouter"]
    assert parse_search(b"{}") == []
    # Hits without a title are dropped, not fatal.
    no_title = json.dumps({"query": {"search": [{"ns": 0}, {"title": "Stripe, Inc."}]}}).encode()
    assert [h["title"] for h in parse_search(no_title)] == ["Stripe, Inc."]


def test_parse_extlinks_roundtrip():
    urls = parse_extlinks(_extlinks_payload(_T1_TITLE, _T1_EXTLINKS))
    assert urls == _T1_EXTLINKS
    # Page without extlinks key and empty payloads yield nothing.
    assert parse_extlinks(_extlinks_payload(_T1_TITLE, [])) == []
    assert parse_extlinks(b"{}") == []


def test_pick_wikipedia_domain_pure_decision():
    out = pick_wikipedia_domain(_T1_TITLE, _T1_EXTLINKS, "Stripe, Inc.")
    assert out["status"] == "resolved"
    assert out["domain"] == "stripe.com"
    out = pick_wikipedia_domain(_T1_TITLE, [], "Stripe, Inc.")
    assert out == {"status": "no_match", "domain": None, "candidates": []}


def test_fetch_error_returns_no_match_without_raising():
    fetcher = FakeFetcher(error=ConnectionError("boom"))
    out = WikipediaExtlinksResolver(fetcher, FakeRegistry()).discover("Stripe, Inc.")
    assert out == {"status": "no_match", "domain": None, "candidates": []}


def test_search_no_hits_returns_no_match_after_single_get():
    fetcher = FakeFetcher({"list=search": _search_payload([])})
    out = WikipediaExtlinksResolver(fetcher, FakeRegistry()).discover("Ghosted")
    assert out == {"status": "no_match", "domain": None, "candidates": []}
    assert len(fetcher.tasks) == 1


def test_blank_name_short_circuits_without_fetch():
    fetcher = FakeFetcher(_stripe_routes(_T1_EXTLINKS))
    resolver = WikipediaExtlinksResolver(fetcher, FakeRegistry())
    assert resolver.discover("")["status"] == "no_match"
    assert resolver.discover("   ")["status"] == "no_match"
    assert fetcher.tasks == []


def test_resolve_all_covers_every_name():
    fetcher = FakeFetcher(_stripe_routes(_T1_EXTLINKS))
    out = WikipediaExtlinksResolver(fetcher, FakeRegistry()).resolve_all(
        ["Stripe, Inc.", ""]
    )
    assert out["Stripe, Inc."]["status"] == "resolved"
    assert out[""]["status"] == "no_match"
    assert set(out) == {"Stripe, Inc.", ""}
