"""Wave D / Task D1: the company feed follows a capped number of article links.

The company_feed adapter reads a company's own blog feed, whose items carry a
title and a ~200-char teaser summary and nothing else — the operational-need
language lives on the ARTICLE pages behind the feed's links. follow_tasks()
therefore queues a capped number of those links (kind="article") so the article
bodies land in the raw store for the `needs` source; the article pages
themselves emit no signals.

The reparse half is here too: reparse() rebuilds task_meta from the stored
document row, so the collector's kind="article" marker is gone by then and the
kind must be re-derived from the document — otherwise an article body is fed to
blog_to_candidates and page chrome becomes junk product_launch candidates.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from src.core.models import Account, Document
from src.sources.content.blog import blog_to_candidates
from src.sources.news.collector import CompanyFeedSource
from src.sources.news.feeds import parse_feed

FIX = Path(__file__).parent / "fixtures" / "news"
FIXTURE = FIX / "company_feed_two_items.xml"
FEED_URL = "https://acme.com/blog/feed"
ARTICLE_1 = "https://acme.com/blog/introducing-widget-3-0"
ARTICLE_2 = "https://acme.com/blog/iso-27001-certification"
TODAY = "2026-08-16"


def _account():
    return Account(domain="acme.com", name="Acme", blog_feed_url=FEED_URL)


def _feed_doc():
    return Document(
        doc_id="f", source="company_feed", url=FEED_URL, body=FIXTURE.read_bytes()
    )


# --------------------------------------------------------------------------
# follow_tasks (cap + link hygiene)
# --------------------------------------------------------------------------


def test_follow_tasks_respects_default_cap():
    """Default config (article_follow_max 5) queues the 2 distinct absolute
    links from the fixture, each stamped kind='article'."""
    tasks = CompanyFeedSource().follow_tasks(_feed_doc(), _account(), {"today": TODAY})
    assert [t.url for t in tasks] == [ARTICLE_1, ARTICLE_2]
    assert all(t.meta["kind"] == "article" for t in tasks)
    assert all(t.meta["link"] == t.url for t in tasks)
    assert all(t.source == "company_feed" and t.domain == "acme.com" for t in tasks)


def test_follow_tasks_skips_relative_and_duplicate_links():
    """The relative link is not followable and the duplicate appears once."""
    tasks = CompanyFeedSource().follow_tasks(_feed_doc(), _account(), {"today": TODAY})
    urls = [t.url for t in tasks]
    assert "/blog/relative" not in " ".join(urls)
    assert all(u.startswith("https://") for u in urls)
    assert urls.count(ARTICLE_1) == 1
    assert len(urls) == len(set(urls)) == 2


def test_follow_tasks_cap_zero_disables_following(monkeypatch):
    """article_follow_max: 0 -> no follow tasks at all."""
    import src.sources.news.collector as collector_mod

    monkeypatch.setattr(
        collector_mod, "load_company_feed_cfg", lambda: {"article_follow_max": 0}
    )
    assert CompanyFeedSource().follow_tasks(_feed_doc(), _account(), {"today": TODAY}) == []


def test_follow_tasks_cap_one_stops_at_first_link(monkeypatch):
    """The cap truncates in feed order and never re-follows an article page."""
    import src.sources.news.collector as collector_mod

    monkeypatch.setattr(
        collector_mod, "load_company_feed_cfg", lambda: {"article_follow_max": 1}
    )
    src = CompanyFeedSource()
    tasks = src.follow_tasks(_feed_doc(), _account(), {"today": TODAY})
    assert [t.url for t in tasks] == [ARTICLE_1]
    article_doc = Document(
        doc_id="a", source="company_feed", url=ARTICLE_1, body=FIXTURE.read_bytes()
    )
    assert src.follow_tasks(article_doc, _account(), {"kind": "article"}) == []


# --------------------------------------------------------------------------
# article pages emit nothing of their own
# --------------------------------------------------------------------------


def test_article_doc_emits_no_signals_and_is_not_refollowed():
    src = CompanyFeedSource()
    account = _account()
    article = Document(
        doc_id="a", source="company_feed", url=ARTICLE_1, body=FIXTURE.read_bytes()
    )
    meta = {"kind": "article", "link": ARTICLE_1, "today": TODAY}
    assert src.follow_tasks(article, account, meta) == []
    assert src.parse(article, account, meta) == []


# --------------------------------------------------------------------------
# regression: the feed document itself parses exactly as before
# --------------------------------------------------------------------------


def test_feed_doc_parse_unchanged_versus_direct_blog_to_candidates():
    src = CompanyFeedSource()
    doc = _feed_doc()
    account = _account()
    got = src.parse(doc, account, {"today": TODAY})
    expected = blog_to_candidates(parse_feed(doc.body), account, today=date(2026, 8, 16))
    assert [c.natural_key for c in got] == [c.natural_key for c in expected]
    types = {c.signal_type for c in got}
    assert "product_launch" in types  # "Introducing Widget 3.0"
    assert "certification" in types  # "ISO 27001 certification"


# --------------------------------------------------------------------------
# reparse kind derivation
# --------------------------------------------------------------------------


def test_company_feed_kind_derives_from_document_url():
    from src.pipeline.orchestrator import _company_feed_kind

    account = _account()
    assert _company_feed_kind(_feed_doc(), account) == "blog"
    # Whitespace-padded stored URL is still the feed.
    padded = Document(doc_id="f2", source="company_feed", url=f" {FEED_URL} ", body=b"x")
    assert _company_feed_kind(padded, account) == "blog"
    article = Document(doc_id="a", source="company_feed", url=ARTICLE_1, body=b"x")
    assert _company_feed_kind(article, account) == "article"
    # No blog_feed_url on the account -> never 'blog'.
    assert _company_feed_kind(_feed_doc(), Account(domain="acme.com", name="Acme")) == "article"
    # Missing document URL with a feed URL on the account -> 'article'.
    assert _company_feed_kind(Document(doc_id="n", source="company_feed"), account) == "article"


def test_reparse_article_doc_produces_no_product_launch(tmp_path):
    """Offline reparse harness (the one tests/test_orchestrator.py uses).

    An article document (URL != the account's blog_feed_url) must not reach
    blog_to_candidates. The article body here is deliberately feed-shaped: that
    is the failure mode the fix addresses (reparse stamped every company_feed
    document kind='blog', so feed-shaped page bytes emitted junk product_launch
    candidates). The probe's URL is unique to the probe, so a signal carrying it
    proves blog_to_candidates ran on the article document.
    """
    from src.core.config import Config
    from src.pipeline.orchestrator import Orchestrator

    cfg = Config()
    cfg.contact_email = "recon@example.com"
    cfg.http.respect_robots = False
    cfg.storage.db_path = str(tmp_path / "s.db")
    cfg.storage.raw_dir = str(tmp_path / "raw")
    cfg.storage.briefs_dir = str(tmp_path / "briefs")
    cfg.storage.export_dir = str(tmp_path / "exports")
    cfg.config_dir = "config"
    orch = Orchestrator(cfg, adapters=[CompanyFeedSource()])
    orch.registry.upsert(_account())

    probe_url = "https://acme.com/blog/nebula-console"
    probe_body = (
        b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
        b"<title>Acme Blog</title>"
        b"<entry><title>Introducing Nebula Console</title>"
        b'<link href="' + probe_url.encode() + b'"/>'
        b"<updated>2026-08-06T00:00:00Z</updated>"
        b"<summary>We launched Nebula Console today.</summary></entry></feed>"
    )
    orch.raw.put(
        source="company_feed",
        url=FEED_URL,
        body=FIXTURE.read_bytes(),
        content_type="application/atom+xml",
        status=200,
        domain="acme.com",
    )
    orch.raw.put(
        source="company_feed",
        url=probe_url,
        body=probe_body,
        content_type="text/html",
        status=200,
        domain="acme.com",
    )

    stats = orch.reparse(sources=["company_feed"])
    assert stats.failed == 0

    types = {r["signal_type"] for r in orch.db.query("SELECT signal_type FROM signals")}
    urls = {r["url"] for r in orch.db.query("SELECT url FROM signals")}
    # The feed document still yields its own candidates...
    assert "product_launch" in types
    assert ARTICLE_1 in urls
    # ...and the article document yields none (probe URL absent everywhere).
    assert probe_url not in urls
    expected_cands = blog_to_candidates(
        parse_feed(FIXTURE.read_bytes()), _account(), today=date(2026, 8, 16)
    )
    assert stats.candidates == len(expected_cands)