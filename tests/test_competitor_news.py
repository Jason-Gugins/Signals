"""Plan T7 — keyless competitor-candidate mining (Bing News RSS + HN Algolia).

Covers:
- query builders: exact verified URL shapes (percent-encoded quotes/spaces)
- parse_rss_titles: real-shaped Bing feeds (CDATA, encoding decl), caps,
  malformed XML -> []
- parse_hn_hits: hits incl. url=None, caps, malformed JSON -> []
- extract_competitor_names: co-mention split rules, self-name exclusion,
  noise/short sides, dedupe by (source, name)
- CompetitorNewsPass: 3-source merge, per-source error isolation, 3-GET
  budget, never raises (FakeFetcher, no network)
- Orchestrator.discover_competitors: persists kind=competitor rows keyed by
  normalize_entity (temp-db pattern from tests/test_identity_candidates.py);
  queued False when no competitors
- CLI: `sweep --competitors` happy path + UsageError on --discover+--competitors
  and on a bare positional target (class-level patch pattern from
  tests/test_sweep_discover.py)
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from src.identity.competitor_news import (
    COMPETITOR_CAP,
    CompetitorNewsPass,
    build_bing_alt_url,
    build_bing_vs_url,
    build_hn_url,
    extract_competitor_names,
    parse_hn_hits,
    parse_rss_titles,
)


# ── fixtures / fake fetcher ──────────────────────────────────────────────────

class FakeDoc:
    def __init__(self, body: bytes):
        self.body = body


class FakeResult:
    def __init__(self, body: bytes | None, ok: bool = True):
        self.ok = ok
        self.doc = FakeDoc(body) if body is not None else None
        self.status = 200 if ok else 404


class FakeFetcher:
    """Routes canned payloads by URL substring (no network).

    error_routes are checked first and raise; a URL matching no route comes
    back ok=False (a route miss is a failed fetch, like a 404).
    """

    def __init__(self, routes: dict[str, bytes] | None = None, error_routes: dict[str, Exception] | None = None):
        self.routes = routes or {}
        self.error_routes = error_routes or {}
        self.tasks: list = []

    def get(self, task):
        self.tasks.append(task)
        for key, exc in self.error_routes.items():
            if key in task.url:
                raise exc
        for key, body in self.routes.items():
            if key in task.url:
                return FakeResult(body, ok=True)
        return FakeResult(None, ok=False)


def _rss(items: list[tuple[str, str]]) -> bytes:
    """Real-shaped Bing News RSS feed (encoding decl, CDATA titles, pubDate)."""
    entries = "".join(
        f"<item><title><![CDATA[{t}]]></title><link>{u}</link>"
        "<pubDate>Wed, 02 Sep 2026 10:00:00 GMT</pubDate>"
        f"<description><![CDATA[{t}]]></description></item>"
        for t, u in items
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<rss version="2.0"><channel><title>Bing News</title>'
        f"{entries}</channel></rss>"
    ).encode("utf-8")


def _hit(title: str | None, url: str | None = "https://example.com/a", oid: str = "42") -> dict:
    return {
        "created_at": "2026-09-01T00:00:00Z",
        "title": title,
        "url": url,
        "author": "jdoe",
        "points": 5,
        "story_text": None,
        "comment_text": None,
        "num_comments": 1,
        "story_id": None,
        "objectID": oid,
    }


def _hn(hits: list[dict]) -> bytes:
    return json.dumps({"hits": hits, "nbHits": len(hits), "page": 0}).encode("utf-8")


def _routes(vs_items=None, alt_items=None, hn_hits=None) -> dict[str, bytes]:
    """URL-routed canned payloads: the two Bing queries and the HN API."""
    routes: dict[str, bytes] = {}
    if vs_items is not None:
        routes["+vs&format=RSS"] = _rss(vs_items)
    if alt_items is not None:
        routes["alternative+to&format=RSS"] = _rss(alt_items)
    if hn_hits is not None:
        routes["hn.algolia.com"] = _hn(hn_hits)
    return routes


# ── query builders ────────────────────────────────────────────────────────────

def test_build_bing_vs_url_matches_verified_shape():
    # Review-round-verified external shape (quotes percent-encoded, + spaces).
    assert build_bing_vs_url("Stripe") == (
        "https://www.bing.com/news/search?q=%22Stripe%22+vs&format=RSS"
    )


def test_build_bing_vs_url_encodes_spaces_and_quotes():
    assert build_bing_vs_url("Acme Corp") == (
        "https://www.bing.com/news/search?q=%22Acme+Corp%22+vs&format=RSS"
    )


def test_build_bing_alt_url_matches_verified_shape():
    assert build_bing_alt_url("Stripe") == (
        "https://www.bing.com/news/search?q=%22Stripe%22+alternative+to&format=RSS"
    )


def test_build_hn_url_boolean_or_query_with_story_tag():
    assert build_hn_url("Stripe") == (
        "https://hn.algolia.com/api/v1/search"
        "?query=%22Stripe%22+vs+OR+%22alternative+to%22+Stripe&tags=story&hitsPerPage=20"
    )


# ── RSS parser ────────────────────────────────────────────────────────────────

def test_parse_rss_titles_bing_feed_shape():
    feed = _rss(
        [
            ("Stripe vs PayPal: payments showdown", "https://bing.example/1"),
            ("PayPal vs Square", "https://bing.example/2"),
        ]
    )
    assert parse_rss_titles(feed) == [
        {"title": "Stripe vs PayPal: payments showdown", "url": "https://bing.example/1"},
        {"title": "PayPal vs Square", "url": "https://bing.example/2"},
    ]


def test_parse_rss_titles_caps_at_10():
    feed = _rss([(f"Acme vs Rival {i}", f"https://bing.example/{i}") for i in range(12)])
    out = parse_rss_titles(feed)
    assert len(out) == 10
    assert out[0]["title"] == "Acme vs Rival 0"
    assert out[-1]["title"] == "Acme vs Rival 9"


def test_parse_rss_titles_missing_link_yields_empty_url():
    xml = (
        '<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel>'
        "<item><title>Acme vs Globe</title></item></channel></rss>"
    ).encode("utf-8")
    assert parse_rss_titles(xml) == [{"title": "Acme vs Globe", "url": ""}]


def test_parse_rss_titles_malformed_xml_returns_empty():
    assert parse_rss_titles(b"<rss><channel><item><title>oops") == []
    assert parse_rss_titles(b"") == []
    assert parse_rss_titles(b"not xml at all") == []


def test_parse_rss_titles_tolerates_encoding_decl():
    xml = (
        '<?xml version="1.0" encoding="ISO-8859-1"?><rss version="2.0"><channel>'
        "<item><title>Acme caf\xe9 vs Globe</title>"
        "<link>https://bing.example/1</link></item></channel></rss>"
    ).encode("iso-8859-1")
    assert parse_rss_titles(xml) == [
        {"title": "Acme caf\xe9 vs Globe", "url": "https://bing.example/1"}
    ]


# ── HN parser ─────────────────────────────────────────────────────────────────

def test_parse_hn_hits_shape():
    payload = _hn([_hit("Stripe vs PayPal", url="https://acme.io"), _hit("Show HN: Acme", url=None, oid="7")])
    assert parse_hn_hits(payload) == [
        {"title": "Stripe vs PayPal", "url": "https://acme.io"},
        {"title": "Show HN: Acme", "url": ""},
    ]


def test_parse_hn_hits_missing_url_keeps_title():
    payload = _hn([_hit("Stripe vs PayPal", url=None)])
    assert parse_hn_hits(payload) == [{"title": "Stripe vs PayPal", "url": ""}]


def test_parse_hn_hits_caps_at_20():
    payload = _hn([_hit(f"Acme vs Rival {i}", oid=str(i)) for i in range(25)])
    out = parse_hn_hits(payload)
    assert len(out) == 20
    assert out[-1]["title"] == "Acme vs Rival 19"


def test_parse_hn_hits_malformed_json_returns_empty():
    assert parse_hn_hits(b"{not json") == []
    assert parse_hn_hits(b"") == []
    assert parse_hn_hits(b"{}") == []
    assert parse_hn_hits(b'{"hits": null}') == []


def test_parse_hn_hits_skips_titleless_hits():
    payload = _hn([_hit(None), _hit("Stripe vs PayPal", url=None)])
    assert parse_hn_hits(payload) == [{"title": "Stripe vs PayPal", "url": ""}]


# ── co-mention extraction ─────────────────────────────────────────────────────

def test_extract_vs_yields_other_side():
    titles = [{"title": "Stripe vs PayPal", "url": "u1", "source": "bing_news"}]
    assert extract_competitor_names(titles, "Stripe") == [
        {
            "name": "paypal",
            "title": "Stripe vs PayPal",
            "url": "u1",
            "source": "bing_news",
            "score": 1,
        }
    ]


def test_extract_alternatives_to_yields_candidate_after_to():
    titles = [{"title": "Alternatives to Stripe for startups", "url": "u2", "source": "hn_algolia"}]
    assert extract_competitor_names(titles, "PayPal") == [
        {
            "name": "stripe",
            "title": "Alternatives to Stripe for startups",
            "url": "u2",
            "source": "hn_algolia",
            "score": 1,
        }
    ]


def test_extract_vs_with_period():
    titles = [{"title": "Stripe vs. PayPal", "url": "u", "source": "bing_news"}]
    assert [c["name"] for c in extract_competitor_names(titles, "Stripe")] == ["paypal"]


def test_extract_cuts_at_headline_punctuation():
    # The colon keeps "payments showdown" from becoming part of the name.
    titles = [{"title": "Stripe vs PayPal: payments showdown", "url": "u", "source": "bing_news"}]
    assert [c["name"] for c in extract_competitor_names(titles, "Stripe")] == ["paypal"]


def test_extract_compared_to_and_competing_with():
    titles = [
        {"title": "Acme compared to Globe", "url": "u1", "source": "bing_news"},
        {"title": "PayPal competing with Stripe in 2026", "url": "u2", "source": "hn_algolia"},
    ]
    out = extract_competitor_names(titles, "Stripe")
    # Both sides of "compared to" carry a name (neither is the self);
    # "competing with" leaves "stripe" on the self side — dropped.
    assert [(c["name"], c["source"]) for c in out] == [
        ("acme", "bing_news"),
        ("globe", "bing_news"),
        ("paypal", "hn_algolia"),
    ]


def test_extract_self_name_never_returned():
    titles = [
        {"title": "Stripe vs PayPal", "url": "u1", "source": "bing_news"},
        {"title": "PayPal vs Stripe", "url": "u2", "source": "hn_algolia"},
        {"title": "Alternatives to Stripe for startups", "url": "u3", "source": "hn_algolia"},
    ]
    out = extract_competitor_names(titles, "STRIPE")
    assert {c["name"] for c in out} == {"paypal"}
    assert all(c["name"] != "stripe" for c in out)


def test_extract_casefold_matching_both_directions():
    out = extract_competitor_names(
        [{"title": "STRIPE vs PayPal", "url": "u", "source": "hn_algolia"}], "stripe"
    )
    assert [c["name"] for c in out] == ["paypal"]
    # Self matched through the normalized form ("STRIPE Inc" -> "stripe").
    out = extract_competitor_names(
        [{"title": "Stripe vs PayPal", "url": "u", "source": "hn_algolia"}], "STRIPE Inc"
    )
    assert [c["name"] for c in out] == ["paypal"]


def test_extract_empty_other_side_dropped():
    assert extract_competitor_names([{"title": "Acme vs", "url": "u", "source": "bing_news"}], "Acme") == []
    assert extract_competitor_names([{"title": "Acme versus", "url": "u", "source": "bing_news"}], "Acme") == []


def test_extract_noise_side_dropped():
    titles = [{"title": "Stripe vs The Best", "url": "u", "source": "bing_news"}]
    assert extract_competitor_names(titles, "Stripe") == []


def test_extract_drops_short_sides():
    titles = [{"title": "Stripe vs AI", "url": "u", "source": "bing_news"}]
    assert extract_competitor_names(titles, "Stripe") == []


def test_extract_never_fabricates_without_pattern():
    titles = [{"title": "Stripe announces new billing suite", "url": "u", "source": "bing_news"}]
    assert extract_competitor_names(titles, "Stripe") == []
    assert extract_competitor_names([], "Stripe") == []


def test_extract_dedupes_by_source_and_name_keeping_first_title():
    titles = [
        {"title": "Stripe vs PayPal", "url": "u1", "source": "bing_news"},
        {"title": "Stripe vs. PayPal: rematch", "url": "u2", "source": "bing_news"},
        {"title": "PayPal versus Stripe", "url": "u3", "source": "hn_algolia"},
    ]
    out = extract_competitor_names(titles, "Stripe")
    assert [(c["source"], c["name"], c["url"]) for c in out] == [
        ("bing_news", "paypal", "u1"),
        ("hn_algolia", "paypal", "u3"),
    ]


# ── the pass (network-backed, offline via FakeFetcher) ────────────────────────

def test_pass_merges_three_sources_within_budget():
    fetcher = FakeFetcher(
        _routes(
            vs_items=[("Stripe vs PayPal", "https://bing.example/1")],
            alt_items=[("Alternatives to Square for sellers", "https://bing.example/2")],
            hn_hits=[_hit("PayPal vs Stripe", url=None)],
        )
    )
    out = CompetitorNewsPass(fetcher).discover("Stripe")
    assert out["status"] == "resolved_candidates"
    assert out["name"] == "Stripe"
    assert out["errors"] == {}
    assert out["competitors"] == [
        {
            "name": "paypal",
            "title": "Stripe vs PayPal",
            "url": "https://bing.example/1",
            "source": "bing_news",
            "score": 1,
        },
        {
            "name": "square",
            "title": "Alternatives to Square for sellers",
            "url": "https://bing.example/2",
            "source": "bing_news",
            "score": 1,
        },
        {
            "name": "paypal",
            "title": "PayPal vs Stripe",
            "url": "",
            "source": "hn_algolia",
            "score": 1,
        },
    ]
    # Hard budget: exactly 3 GETs (bing vs, bing alt, HN), all tagged
    # source="competitor_news" for the HttpFetcher/raw-store/logs.
    assert len(fetcher.tasks) == 3
    assert all(t.source == "competitor_news" for t in fetcher.tasks)
    assert fetcher.tasks[0].url == build_bing_vs_url("Stripe")
    assert fetcher.tasks[1].url == build_bing_alt_url("Stripe")
    assert fetcher.tasks[2].url == build_hn_url("Stripe")


def test_pass_one_source_erroring_continues():
    fetcher = FakeFetcher(
        _routes(
            alt_items=[("Alternatives to Square for sellers", "https://bing.example/2")],
            hn_hits=[_hit("Square versus PayPal", url="https://hn.example/1")],
        ),
        error_routes={"+vs&format=RSS": ConnectionError("boom")},
    )
    out = CompetitorNewsPass(fetcher).discover("Stripe")
    assert out["status"] == "resolved_candidates"
    assert out["errors"] == {"bing_vs": "boom"}
    names = {(c["source"], c["name"]) for c in out["competitors"]}
    assert ("bing_news", "square") in names
    assert ("hn_algolia", "paypal") in names
    assert ("hn_algolia", "square") in names


def test_pass_all_sources_failing_returns_no_match_with_errors():
    fetcher = FakeFetcher({})  # every URL misses a route -> ok=False
    out = CompetitorNewsPass(fetcher).discover("Stripe")
    assert out["status"] == "no_match"
    assert out["competitors"] == []
    assert set(out["errors"]) == {"bing_vs", "bing_alt", "hn"}
    assert len(fetcher.tasks) == 3  # budget spent, never raised


def test_pass_fetcher_raising_never_raises():
    fetcher = FakeFetcher(
        error_routes={
            "+vs&format=RSS": ConnectionError("net down"),
            "alternative+to&format=RSS": ConnectionError("net down"),
            "hn.algolia.com": ConnectionError("net down"),
        }
    )
    out = CompetitorNewsPass(fetcher).discover("Stripe")
    assert out["status"] == "no_match"
    assert out["competitors"] == []
    assert set(out["errors"]) == {"bing_vs", "bing_alt", "hn"}


def test_pass_clean_fetch_with_no_comparison_titles_is_no_match():
    fetcher = FakeFetcher(
        _routes(
            vs_items=[("Stripe raises prices again", "https://bing.example/1")],
            alt_items=[("Stripe announces new billing suite", "https://bing.example/2")],
            hn_hits=[_hit("Stripe launches in Brazil", url=None)],
        )
    )
    out = CompetitorNewsPass(fetcher).discover("Stripe")
    assert out["status"] == "no_match"
    assert out["competitors"] == []
    assert out["errors"] == {}


def test_pass_zero_item_feeds_record_notes_not_silence():
    """Wave-2 review fix: a 200-OK feed that parses to zero items must be
    distinguishable from a legitimately empty result (soft-block triage)."""
    fetcher = FakeFetcher(
        _routes(
            vs_items=[("Stripe vs PayPal", "https://bing.example/1")],
            alt_items=[],  # fetched ok, zero items
            hn_hits=[],  # fetched ok, zero items
        )
    )
    out = CompetitorNewsPass(fetcher).discover("Stripe")
    assert out["status"] == "resolved_candidates"
    assert out["notes"] == {
        "bing_alt": "fetched ok, parsed 0 items (possible soft block or dry feed)",
        "hn": "fetched ok, parsed 0 items (possible soft block or dry feed)",
    }
    assert "bing_vs" not in out["notes"]


def test_pass_extract_output_capped_at_competitor_cap():
    """Wave-2 review fix: explicit per-name output bound (first-seen wins)."""
    titles = [
        {"title": f"Stripe vs Rival{i}", "url": f"https://bing.example/{i}", "source": "bing_news"}
        for i in range(40)
    ]
    out = extract_competitor_names(titles, "Stripe")
    assert len(out) == COMPETITOR_CAP
    assert out[0]["name"] == "rival0"
    assert out[-1]["name"] == f"rival{COMPETITOR_CAP - 1}"


def test_pass_budget_drift_fails_loudly_as_error(monkeypatch):
    """Wave-2 review fix: adding a 4th source without raising the constant
    must fail loudly, not silently spend a 4th GET."""
    import src.identity.competitor_news as cn

    monkeypatch.setattr(cn, "MAX_FETCHES_PER_NAME", 2)
    out = CompetitorNewsPass(FakeFetcher(_routes())).discover("Stripe")
    assert out["status"] == "no_match"
    assert "budget drift" in out["errors"]["pass"]


def test_pass_blank_name_short_circuits_without_fetch():
    fetcher = FakeFetcher(_routes(vs_items=[("Stripe vs PayPal", "u")]))
    passer = CompetitorNewsPass(fetcher)
    assert passer.discover("")["status"] == "no_match"
    assert passer.discover("   ")["status"] == "no_match"
    assert fetcher.tasks == []


# ── orchestrator wiring (temp db) ─────────────────────────────────────────────

def _orch(tmp_path: Path, fetcher):
    from src.core.config import Config
    from src.pipeline.orchestrator import Orchestrator

    cfg = Config()
    cfg.contact_email = "ops@example.com"
    cfg.storage.db_path = str(tmp_path / "s.db")
    cfg.storage.raw_dir = str(tmp_path / "raw")
    cfg.storage.briefs_dir = str(tmp_path / "briefs")
    cfg.storage.export_dir = str(tmp_path / "exports")
    cfg.config_dir = "config"
    return Orchestrator(cfg, fetcher=fetcher)


def test_orchestrator_persists_kind_competitor_rows_with_normalized_key(tmp_path: Path):
    fetcher = FakeFetcher(
        _routes(
            vs_items=[("Stripe vs PayPal", "https://bing.example/1")],
            alt_items=[("Stripe announces new billing suite", "https://bing.example/2")],
            hn_hits=[_hit("Stripe launches in Brazil", url=None)],
        )
    )
    orch = _orch(tmp_path, fetcher)
    try:
        result = orch.discover_competitors("STRIPE, Inc.")
        assert result["status"] == "resolved_candidates"
        assert result["queued"] is True
        assert result["errors"] == {}
        assert [c["name"] for c in result["competitors"]] == ["paypal"]

        row = orch.db.one("SELECT * FROM identity_candidates")
        assert row is not None
        # Store key is the normalize_entity form ("STRIPE, Inc." -> "stripe"),
        # mirroring discover(); kind/source per the T6-carried mechanism.
        assert row["name"] == "stripe"
        assert row["kind"] == "competitor"
        assert row["source"] == "competitor_news"
        assert row["status"] == "pending"
        assert json.loads(row["candidates_json"]) == result["competitors"]
        # RunContext stage recorded for the run log.
        assert orch.db.one("SELECT stage FROM runs WHERE stage = 'discover_competitors'")
    finally:
        orch.db.close()


def test_orchestrator_no_competitors_is_not_queued(tmp_path: Path):
    fetcher = FakeFetcher(
        _routes(vs_items=[("Stripe raises prices again", "https://bing.example/1")])
    )
    orch = _orch(tmp_path, fetcher)
    try:
        result = orch.discover_competitors("Stripe")
        assert result["status"] == "no_match"
        assert result["queued"] is False
        assert result["competitors"] == []
        assert orch.db.one("SELECT * FROM identity_candidates") is None
    finally:
        orch.db.close()


def test_orchestrator_total_failure_never_raises_and_queues_nothing(tmp_path: Path):
    orch = _orch(tmp_path, FakeFetcher(error_routes={
        "+vs&format=RSS": ConnectionError("net down"),
        "alternative+to&format=RSS": ConnectionError("net down"),
        "hn.algolia.com": ConnectionError("net down"),
    }))
    try:
        result = orch.discover_competitors("Stripe")
        assert result["queued"] is False
        assert result["competitors"] == []
        assert set(result["errors"]) == {"bing_vs", "bing_alt", "hn"}
        assert orch.db.one("SELECT * FROM identity_candidates") is None
    finally:
        orch.db.close()


# ── CLI: sweep --competitors ──────────────────────────────────────────────────

def _fake_result(name="Stripe", competitors=None, queued=None) -> dict:
    if competitors is None:
        competitors = [
            {
                "name": "paypal",
                "title": "Stripe vs PayPal",
                "url": "https://bing.example/1",
                "source": "bing_news",
                "score": 1,
            },
            {
                "name": "square",
                "title": "Square versus PayPal",
                "url": "https://hn.example/2",
                "source": "hn_algolia",
                "score": 1,
            },
        ]
    return {
        "name": name,
        "status": "resolved_candidates" if competitors else "no_match",
        "competitors": competitors,
        "queued": bool(competitors) if queued is None else queued,
        "errors": {},
    }


def _patch_competitors(monkeypatch, results=None, error_for=None):
    """Class-level Orchestrator.discover_competitors patch (Boom-contract style)."""
    from src.pipeline.orchestrator import Orchestrator

    calls = []

    def fake_discover_competitors(self, name):
        calls.append({"name": name})
        if error_for and name in error_for:
            raise error_for[name]
        table = results() if callable(results) else results
        if isinstance(table, dict):
            return table[name]
        return _fake_result(name=name)

    monkeypatch.setattr(Orchestrator, "discover_competitors", fake_discover_competitors)
    return calls


def _setup(monkeypatch):
    from src.pipeline.orchestrator import Orchestrator

    monkeypatch.setattr(Orchestrator, "__init__", lambda self, *a, **k: None)


def _forbid_run_sweep(monkeypatch):
    from src.pipeline import sweep as sweep_mod

    def boom(*a, **kw):
        raise AssertionError("run_sweep must not run in --competitors mode")

    monkeypatch.setattr(sweep_mod, "run_sweep", boom)


def test_cli_sweep_competitors_happy_path(monkeypatch):
    from src.cli import main

    calls = _patch_competitors(monkeypatch)
    _forbid_run_sweep(monkeypatch)
    _setup(monkeypatch)

    result = CliRunner().invoke(main, ["sweep", "--competitors", "Stripe"])

    assert result.exit_code == 0, result.output
    assert calls == [{"name": "Stripe"}]
    assert "Stripe: 2 competitor candidates" in result.output
    assert "  paypal\tbing_news\tStripe vs PayPal\thttps://bing.example/1" in result.output
    assert "  square\thn_algolia\tSquare versus PayPal\thttps://hn.example/2" in result.output
    assert (
        "queued to identity_candidates (kind=competitor); promote by adding to "
        "config/lists/competitors.txt or fingerprints.yaml" in result.output
    )


def test_cli_sweep_competitors_zero_candidates_not_queued(monkeypatch):
    from src.cli import main

    calls = _patch_competitors(
        monkeypatch, results=lambda: {"Stripe": _fake_result(competitors=[])}
    )
    _forbid_run_sweep(monkeypatch)
    _setup(monkeypatch)

    result = CliRunner().invoke(main, ["sweep", "--competitors", "Stripe"])

    assert result.exit_code == 0, result.output
    assert calls == [{"name": "Stripe"}]
    assert "Stripe: 0 competitor candidates" in result.output
    assert "queued to identity_candidates" not in result.output


def test_cli_sweep_competitors_multiple_names_and_failure_isolation(monkeypatch):
    from src.cli import main

    calls = _patch_competitors(
        monkeypatch,
        results=lambda: {"Good": _fake_result(name="Good")},
        error_for={"Bad": RuntimeError("boom")},
    )
    _forbid_run_sweep(monkeypatch)
    _setup(monkeypatch)

    result = CliRunner().invoke(
        main, ["sweep", "--competitors", "Bad", "--competitors", "Good"]
    )

    assert result.exit_code == 0, result.output
    assert [c["name"] for c in calls] == ["Bad", "Good"]
    assert "Bad: competitor mining failed: boom" in result.output
    assert "Good: 2 competitor candidates" in result.output


def test_cli_sweep_competitors_conflicts_with_discover(monkeypatch):
    from src.cli import main

    calls = _patch_competitors(monkeypatch)
    _setup(monkeypatch)

    result = CliRunner().invoke(main, ["sweep", "--discover", "Acme", "--competitors", "Stripe"])

    assert result.exit_code != 0
    assert "not both" in result.output
    assert calls == []


def test_cli_sweep_competitors_conflicts_with_positional_target(monkeypatch):
    from src.cli import main

    calls = _patch_competitors(monkeypatch)
    _setup(monkeypatch)

    result = CliRunner().invoke(main, ["sweep", "acme.io", "--competitors", "Stripe"])

    assert result.exit_code != 0
    assert "not both" in result.output
    assert calls == []


def test_cli_sweep_competitors_never_creates_accounts(monkeypatch):
    from src.cli import main
    from src.identity.registry import AccountRegistry

    _patch_competitors(monkeypatch)
    _forbid_run_sweep(monkeypatch)
    _setup(monkeypatch)
    upserts = []
    monkeypatch.setattr(
        AccountRegistry, "upsert", lambda self, account, **kw: upserts.append(account)
    )

    result = CliRunner().invoke(main, ["sweep", "--competitors", "Stripe"])

    assert result.exit_code == 0, result.output
    assert upserts == []
