"""Tests for the DDG SERP domain discovery resolver (fake fetcher/seam
patch, no network)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import src.identity.ddg_ids as ddg
from src.identity.ddg_ids import (
    DDG_PACE_S,
    DdgSerpResolver,
    _score_candidate,
    build_search_url,
    parse_results,
    pick_ddg_candidate,
)


class FakeResponse:
    """CurlCffiFetcher-shaped canned response (.status/.body)."""

    def __init__(self, body: str | bytes | None, status: int = 200):
        if isinstance(body, str):
            body = body.encode()
        self.body = body
        self.status = status


class FakeFetcher:
    """Injectable fetcher object returning one canned body (no network)."""

    def __init__(
        self,
        body: str | bytes = "",
        status: int = 200,
        error: Exception | None = None,
    ):
        self.body = body
        self.status = status
        self.error = error
        self.urls: list[str] = []

    def get(self, url: str):
        if self.error is not None:
            raise self.error
        self.urls.append(url)
        return FakeResponse(self.body, self.status)


class FakeRegistry:
    def __init__(self):
        self.upserts: list = []

    def upsert(self, account, *, source=None):
        self.upserts.append((account, source))
        return account


class FakeClock:
    """Scripted monotonic times; repeats the last value once exhausted."""

    def __init__(self, times: list[float]):
        self.times = list(times)
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        if len(self.times) > 1:
            return self.times.pop(0)
        return self.times[0]


def _anchor(title: str, href: str) -> str:
    return f'<a rel="nofollow" class="result__a" href="{href}">{title}</a>'


def _uddg(target: str) -> str:
    # T1 probe shape: //duckduckgo.com/l/?uddg=<urlencoded>&rut=...
    return "//duckduckgo.com/l/?uddg=" + _quote(target) + "&amp;rut=357ca1"


def _quote(target: str) -> str:
    from urllib.parse import quote_plus

    return quote_plus(target)


def _t1_success_html() -> str:
    """T1-shaped curl-tier SERP: uddg-wrapped #1, direct #2, wrapped #3."""
    return (
        "<html><body><div class='result'>"
        + _anchor(
            "Stripe | Financial Infrastructure to Grow Your Revenue",
            _uddg("https://stripe.com/"),
        )
        + "</div><div class='result'>"
        + _anchor(
            "Online payment processing for internet businesses - Stripe",
            "https://stripe.com/payments",
        )
        + "</div><div class='result'>"
        + _anchor("Dashboard | Stripe", _uddg("https://dashboard.stripe.com/"))
        + "</div></body></html>"
    )


def test_resolved_on_first_result_matching_name():
    """(a) #1 uddg-wrapped result matches the name -> resolved, strict rule."""
    out = DdgSerpResolver(FakeFetcher(_t1_success_html())).discover("Stripe")
    assert out["status"] == "resolved"
    assert out["domain"] == "stripe.com"
    assert out["candidates"] == [
        {
            "position": 1,
            "title": "Stripe | Financial Infrastructure to Grow Your Revenue",
            "url": "https://stripe.com/",
            "domain": "stripe.com",
            "score": 4,  # +2 strict #1+name match, +2 token overlap
        },
        {
            "position": 2,
            "title": "Online payment processing for internet businesses - Stripe",
            "url": "https://stripe.com/payments",
            "domain": "stripe.com",
            "score": 2,
        },
        {
            "position": 3,
            "title": "Dashboard | Stripe",
            "url": "https://dashboard.stripe.com/",
            "domain": "stripe.com",
            "score": 2,
        },
    ]


def test_plain_callable_fetcher_is_accepted():
    """The injectable callable arm (url -> html) works like capterra_resolve."""
    out = DdgSerpResolver(fetcher=lambda url: _t1_success_html()).discover("Stripe")
    assert out["status"] == "resolved"
    assert out["domain"] == "stripe.com"


def test_challenge_body_with_200_records_marker():
    """(b) HTTP 200 can still be a challenge — body wins, marker recorded."""
    body = (
        "<html><body>Unfortunately, bots use DuckDuckGo too. "
        "Please complete the following puzzle.</body></html>"
    )
    out = DdgSerpResolver(FakeFetcher(body, status=200)).discover("Stripe")
    assert out["status"] == "error"
    assert out["domain"] is None
    assert out["candidates"] == []
    assert out["error"] == (
        "ddg challenge detected: unfortunately, bots use duckduckgo"
    )


def test_challenge_body_with_202_records_marker():
    """(b) The 202 variant — same error contract, different marker."""
    body = "<html><body>anomaly-detected: suspicious traffic from your network</body></html>"
    out = DdgSerpResolver(FakeFetcher(body, status=202)).discover("Stripe")
    assert out["status"] == "error"
    assert out["candidates"] == []
    assert out["error"] == "ddg challenge detected: anomaly-detected"


def test_first_result_wikipedia_is_ambiguous_and_ranked():
    """(c) #1 wikipedia.org: non-official penalty + no strict match -> ambiguous."""
    html = (
        "<html><body>"
        + _anchor(
            "Stripe, Inc. - Wikipedia",
            _uddg("https://en.wikipedia.org/wiki/Stripe_Inc."),
        )
        + _anchor("Stripe | Financial Infrastructure", _uddg("https://stripe.com/"))
        + "</body></html>"
    )
    out = DdgSerpResolver(FakeFetcher(html)).discover("Stripe")
    assert out["status"] == "ambiguous"
    assert out["domain"] is None  # display-only ranking, never auto-picks
    assert [(c["domain"], c["score"]) for c in out["candidates"]] == [
        ("stripe.com", 2),
        ("wikipedia.org", -1),  # -1 non-official penalty
    ]


def test_no_results_returns_no_match():
    """(d) A marker-free body with no result__a anchors is a clean no_match."""
    html = "<html><body><nav><a href='/about'>About DDG</a></nav></body></html>"
    out = DdgSerpResolver(FakeFetcher(html)).discover("Stripe")
    assert out == {"status": "no_match", "domain": None, "candidates": []}


def test_empty_body_returns_no_match():
    out = DdgSerpResolver(FakeFetcher(b"")).discover("Stripe")
    assert out == {"status": "no_match", "domain": None, "candidates": []}


def test_uddg_unquoting_and_direct_passthrough():
    """(e) %3A/%2F uddg targets are unquoted; doubly encoded get a second pass."""
    html = (
        "<html><body>"
        + _anchor("Example", _uddg("https://example.io/path?q=1"))
        + _anchor("Acme", _uddg("https%3A%2F%2Facme.tv%2F"))  # doubly encoded
        + _anchor("Direct", "https://directexample.org/")
        + "</body></html>"
    )
    results = parse_results(html)
    assert [(r["position"], r["url"], r["domain"]) for r in results] == [
        (1, "https://example.io/path?q=1", "example.io"),
        (2, "https://acme.tv/", "acme.tv"),
        (3, "https://directexample.org/", "directexample.org"),
    ]


def test_parse_results_skips_ads_internal_and_non_http():
    html = (
        "<html><body>"
        + _anchor("Ad one", "//duckduckgo.com/y.js?ad_domain=ads.example.com&amp;ad_provider=baz")
        + _anchor(
            "Ad two",
            "//duckduckgo.com/l/?uddg=https%3A%2F%2Fpromoted.example.com%2F&amp;ad_domain=promoted.example.com",
        )
        + _anchor("Scripted", "javascript:void(0)")
        + _anchor("Files", "ftp://files.example.com/pub")
        + _anchor("Internal", _uddg("https://duckduckgo.com/about"))
        + _anchor("No href", "")
        + _anchor("Acme", "https://www.acme.io/products")
        + _anchor("Acme dup", "https://www.acme.io/products")  # deduped
        + "</body></html>"
    )
    results = parse_results(html)
    assert results == [
        {
            "position": 1,
            "title": "Acme",
            "url": "https://www.acme.io/products",
            "domain": "acme.io",
            "score": 0,
        }
    ]


def test_parse_results_caps_at_ten():
    html = "<html><body>" + "".join(
        _anchor(f"Site {i}", f"https://site{i}.example/") for i in range(1, 13)
    ) + "</body></html>"
    results = parse_results(html)
    assert len(results) == 10
    assert [r["position"] for r in results] == list(range(1, 11))
    assert results[-1]["domain"] == "site10.example"


def test_pace_sleeps_between_calls_within_window():
    """(f) Second call inside DDG_PACE_S sleeps the remaining interval."""
    fetcher = FakeFetcher(_t1_success_html())
    clock = FakeClock([100.0, 102.0, 106.0])
    sleeps: list[float] = []
    resolver = DdgSerpResolver(fetcher, clock=clock, sleep=sleeps.append)
    resolver.discover("Stripe")
    resolver.discover("Stripe")
    resolver.discover("Stripe")
    assert sleeps == [DDG_PACE_S - 2.0, 4.0]  # 102 is 2s after 100; then 4s
    assert len(fetcher.urls) == 3  # pacing delays, it never skips the call


def test_pace_skips_sleep_after_interval_elapsed():
    fetcher = FakeFetcher(_t1_success_html())
    clock = FakeClock([100.0, 105.0])  # exactly DDG_PACE_S apart
    sleeps: list[float] = []
    resolver = DdgSerpResolver(fetcher, clock=clock, sleep=sleeps.append)
    resolver.discover("Stripe")
    resolver.discover("Stripe")
    assert sleeps == []
    assert len(fetcher.urls) == 2


def test_blank_name_short_circuits_without_fetch_or_clock():
    """(g) Blank names never reach the network or even the clock."""
    fetcher = FakeFetcher(_t1_success_html())
    clock = FakeClock([0.0])
    resolver = DdgSerpResolver(fetcher, clock=clock, sleep=lambda s: None)
    for blank in ("", "   "):
        assert resolver.discover(blank) == {
            "status": "no_match",
            "domain": None,
            "candidates": [],
        }
    assert fetcher.urls == []
    assert clock.calls == 0


def test_build_search_url_encodes_query():
    url = build_search_url("Stripe, Inc.")
    assert url == "https://html.duckduckgo.com/html/?q=Stripe%2C+Inc.+official+website"


def test_score_candidate_table():
    """+2 strict #1+name, +2 token overlap, -1 non-official root (x.com too)."""
    assert _score_candidate({"position": 1, "domain": "stripe.com"}, "Stripe") == 4
    assert _score_candidate({"position": 2, "domain": "stripe.com"}, "Stripe") == 2
    assert _score_candidate({"position": 1, "domain": "wikipedia.org"}, "Stripe") == -1
    assert _score_candidate({"position": 1, "domain": "x.com"}, "Stripe") == -1
    assert _score_candidate({"position": 3, "domain": "unrelated.io"}, "Stripe") == 0


def test_pick_tie_never_auto_picks():
    """Strict #1 candidate tied on score with a rival -> ambiguous (no guess)."""
    candidates = [
        {"position": 1, "title": "Acmeo", "url": "https://acme.com/", "domain": "acme.com"},
        {"position": 2, "title": "Acmeo", "url": "https://acmeo.io/", "domain": "acmeo.io"},
    ]
    out = pick_ddg_candidate(candidates, "Acmeo")
    assert out["status"] == "ambiguous"  # both score 2: strict+no-overlap vs overlap
    assert out["domain"] is None
    assert [c["domain"] for c in out["candidates"]] == ["acme.com", "acmeo.io"]


def test_fetch_exception_returns_error_without_raising():
    out = DdgSerpResolver(FakeFetcher(error=ConnectionError("boom"))).discover("Stripe")
    assert out["status"] == "error"
    assert out["domain"] is None
    assert out["candidates"] == []
    assert "ddg fetch failed" in out["error"]


def test_default_fetcher_uses_curl_cffi_get_seam():
    """No fetcher injected -> lazy CurlCffiFetcher through the patch seam."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = _t1_success_html().encode()
    mock_response.cookies = {}
    mock_response.headers = {}
    mock_response.url = "https://html.duckduckgo.com/html/?q=Stripe+official+website"
    with patch(
        "src.core.curl_fetcher.curl_cffi_get", return_value=mock_response
    ) as mock_get:
        out = DdgSerpResolver().discover("Stripe")
    assert out["status"] == "resolved"
    assert out["domain"] == "stripe.com"
    assert mock_get.call_count == 1
    url = mock_get.call_args.args[0]
    kwargs = mock_get.call_args.kwargs
    assert url == "https://html.duckduckgo.com/html/?q=Stripe+official+website"
    assert kwargs["impersonate"] == "chrome"
    assert kwargs["timeout"] == 30


def test_discover_never_raises_on_parser_bug(monkeypatch):
    def boom(html):
        raise RuntimeError("boom")

    monkeypatch.setattr(ddg, "parse_results", boom)
    out = DdgSerpResolver(FakeFetcher(_t1_success_html())).discover("Stripe")
    assert out == {"status": "no_match", "domain": None, "candidates": []}


def test_registry_is_accepted_but_never_written():
    registry = FakeRegistry()
    resolver = DdgSerpResolver(FakeFetcher(_t1_success_html()), registry)
    out = resolver.resolve_all(["Stripe", ""])
    assert set(out) == {"Stripe", ""}
    assert out["Stripe"]["status"] == "resolved"
    assert out[""]["status"] == "no_match"
    assert registry.upserts == []
