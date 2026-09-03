"""Tests for optional rerank wiring in the news collectors (plan Task 3).

The rerank gate is disabled by default: parse() must be byte-identical to
the pre-rerank behavior. When enabled via config (task_meta["rerank"] or
config/default.yaml -> rerank), a relevance floor drops below-floor items
and survivors carry evidence_data["relevance"] — natural_key untouched.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.core.models import Account, Document
from src.sources.news import collector as collector_mod
from src.sources.news.collector import GoogleNewsSource, NewsRssSource

TECH_FEED = Path("tests/fixtures/news/google_news_technology.xml").read_bytes()
GNEWS_FEED = Path("tests/fixtures/news/google_news.xml").read_bytes()

ACCOUNT = Account(domain="acme.com", name="Acme Corp")
TODAY = "2026-08-23"


class FakeScorer:
    """Returns a canned score list regardless of input."""

    def __init__(self, scores):
        self.scores = scores
        self.calls = []

    def score_pairs(self, query, docs):
        self.calls.append((query, list(docs)))
        return list(self.scores)


def _parse(src, body, meta, account=ACCOUNT):
    return src.parse(
        Document(doc_id="d", source=src.key, body=body),
        account,
        meta,
    )


# ---------------------------------------------------------------- disabled


def test_disabled_by_default_is_byte_identical():
    """No rerank in task_meta -> parse output identical to pre-rerank baseline
    and the scorer is never constructed."""
    src = GoogleNewsSource()

    def boom():
        raise AssertionError("get_scorer must not be called when disabled")

    monkey_spy = pytest.MonkeyPatch()
    monkey_spy.setattr(collector_mod, "get_scorer", boom)
    try:
        cands = _parse(
            src,
            TECH_FEED,
            {"kind": "search", "query": "Acme Corp", "today": TODAY},
        )
    finally:
        monkey_spy.undo()
    types = sorted(c.signal_type for c in cands)
    assert types == ["exec_hire", "funding_round"]
    # no relevance evidence injected
    assert all("relevance" not in (c.evidence_data or {}) for c in cands)


def test_explicit_enabled_false_is_byte_identical():
    src = GoogleNewsSource()
    meta = {"kind": "search", "query": "Acme Corp", "today": TODAY,
            "rerank": {"enabled": False}}
    cands = _parse(src, TECH_FEED, meta)
    types = sorted(c.signal_type for c in cands)
    assert types == ["exec_hire", "funding_round"]


# ----------------------------------------------------------------- enabled


def test_enabled_drops_below_floor_and_attaches_evidence(monkeypatch):
    fake = FakeScorer([0.9, 0.2, 0.8])
    monkeypatch.setattr(collector_mod, "get_scorer", lambda: fake)
    src = GoogleNewsSource()
    meta = {"kind": "search", "query": "Acme Corp", "today": TODAY,
            "rerank": {"enabled": True, "floor": 0.35}}
    cands = _parse(src, TECH_FEED, meta)
    # exec_hire item scored 0.2 -> dropped by floor
    assert [c.signal_type for c in cands] == ["funding_round"]
    survivor = cands[0]
    assert survivor.evidence_data["relevance"] == pytest.approx(0.9)
    # natural_key stability: same link -> same key
    from src.core.textutil import sha256_hex
    from urllib.parse import urlparse, urlunparse
    p = urlparse("https://techcrunch.com/acme-series-b")
    expected = sha256_hex(urlunparse((p.scheme, p.netloc, p.path, "", "", "")))[:16]
    assert survivor.natural_key == expected


def test_enabled_sends_query_and_stripped_titles(monkeypatch):
    fake = FakeScorer([0.9, 0.2, 0.8])
    monkeypatch.setattr(collector_mod, "get_scorer", lambda: fake)
    src = GoogleNewsSource()
    meta = {"kind": "serp", "query": "Acme Corp", "keyword": "fundraising",
            "today": TODAY, "rerank": {"enabled": True}}
    _parse(src, TECH_FEED, meta)
    query, docs = fake.calls[0]
    # account name + SERP keyword
    assert query == "Acme Corp fundraising"
    # attribution-stripped titles, paired with summaries
    assert docs[0][0] == "Acme Corp raises $50M Series B"


def test_falls_back_to_account_name_without_keyword(monkeypatch):
    fake = FakeScorer([0.9])
    monkeypatch.setattr(collector_mod, "get_scorer", lambda: fake)
    src = NewsRssSource()
    meta = {"kind": "gnews", "today": "2026-08-16",
            "rerank": {"enabled": True}}
    cands = _parse(
        src, GNEWS_FEED, meta, Account(domain="acme.com", name="Acme")
    )
    assert fake.calls[0][0] == "Acme"
    assert [c.signal_type for c in cands] == ["funding_round"]
    assert cands[0].evidence_data["relevance"] == pytest.approx(0.9)


def test_floor_override_from_config(monkeypatch):
    fake = FakeScorer([0.5, 0.9])
    monkeypatch.setattr(collector_mod, "get_scorer", lambda: fake)
    src = NewsRssSource()
    meta = {"kind": "gnews", "today": "2026-08-16",
            "rerank": {"enabled": True, "floor": 0.75}}
    # Reuse the tech feed via NewsRssSource: two Acme items score 0.5 and 0.9;
    # a 0.75 floor keeps only the 0.9 one.
    cands = _parse(src, TECH_FEED, meta, Account(domain="acme.com", name="Acme Corp"))
    assert len(cands) == 1
    assert cands[0].evidence_data["relevance"] == pytest.approx(0.9)


def test_scorer_exception_never_fails_parse(monkeypatch):
    class ExplodingScorer:
        def score_pairs(self, query, docs):
            raise RuntimeError("model exploded")

    monkeypatch.setattr(collector_mod, "get_scorer", lambda: ExplodingScorer())
    src = GoogleNewsSource()
    meta = {"kind": "search", "query": "Acme Corp", "today": TODAY,
            "rerank": {"enabled": True}}
    cands = _parse(src, TECH_FEED, meta)  # must not raise
    types = sorted(c.signal_type for c in cands)
    assert types == ["exec_hire", "funding_round"]


def test_none_scores_keep_everything(monkeypatch):
    """NullScorer-style None result -> no dropping, no evidence."""
    monkeypatch.setattr(collector_mod, "get_scorer", lambda: collector_mod.NullScorer())
    src = GoogleNewsSource()
    meta = {"kind": "search", "query": "Acme Corp", "today": TODAY,
            "rerank": {"enabled": True}}
    cands = _parse(src, TECH_FEED, meta)
    types = sorted(c.signal_type for c in cands)
    assert types == ["exec_hire", "funding_round"]
    assert all("relevance" not in (c.evidence_data or {}) for c in cands)


# --------------------------------------------- domain-proof query boost (A4)


def test_domain_proof_query_boost(monkeypatch):
    """When an item's publisher_domain matches the account domain, the
    reranker query becomes 'Name domain official' instead of 'Name keyword'."""
    fake = FakeScorer([0.9, 0.2, 0.8])
    monkeypatch.setattr(collector_mod, "get_scorer", lambda: fake)
    src = GoogleNewsSource()
    meta = {"kind": "search", "query": "Acme Corp", "today": TODAY,
            "rerank": {"enabled": True}}
    # Feed items whose publisher_domain matches the account domain.
    from src.sources.news.feeds import NewsItem as NI
    items = [
        NI(title="Acme Corp raises $50M Series B", link="https://a/1",
           published="2026-08-18", summary="s", source_name="TechCrunch",
           publisher_domain="acme.com"),
        NI(title="Acme Corp appoints new CTO", link="https://a/2",
           published="2026-08-17", summary="s", source_name="VentureBeat",
           publisher_domain="techcrunch.com"),
        NI(title="Acme Corp opens office", link="https://a/3",
           published="2026-08-16", summary="s", source_name="Verge",
           publisher_domain="www.acme.com"),
    ]
    kept, _ = collector_mod._rerank_items(items, ACCOUNT, meta)
    query, _docs = fake.calls[0]
    assert query == "Acme Corp acme.com official"
    # Default floor 0.35: the 0.2-scored item is dropped, 0.9/0.8 survive.
    assert len(kept) == 2
