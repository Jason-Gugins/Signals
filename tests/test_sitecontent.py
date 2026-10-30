"""sitecontent — sitemap-enumerated first-party prose pages (spike arm C).

The sourcing spike (``data/probe/SOURCING_SPIKE_20261030.md``) verdicts that
gate this adapter: arm C GO — rillet case-study/compare pages are
server-rendered 200s with real prose (84-93KB stripped); arm B NO-GO — no RSS
exists, so ``/blog/`` URLs fold into this adapter's pattern list. The docs'
value is the STORED TEXT for the implement/claim pipeline (the extraction
validation proved the corpus lacks need-bearing prose because these pages
were listed by the sitemap but never fetched); the signal, when the prose
clears the floor, is a thin ``content_appearance`` marker (the taxonomy type
the sibling content sources — itunes/blog/linkedin posts — already use for
first-party content).

All tests OFFLINE: plan() reads a tmp Database + RawStore seeded with a
synthetic sitemap document (the runner injects neither into plan(), so the
adapter resolves them through an injectable context factory — tests pass the
handles directly); parse() is bytes-in-candidates-out.
"""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from src.core.db import Database
from src.core.models import Account, Document
from src.core.rawstore import RawStore
from src.sources.sitecontent.collector import (
    SiteContentSource,
    extract_locs,
    filter_patterns,
    page_prose,
)

DOMAIN = "rillet.com"
SITEMAP_URL = "https://www.rillet.com/sitemap.xml"
TODAY = "2026-10-30"

SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.rillet.com/</loc></url>
  <url><loc>https://www.rillet.com/pricing</loc></url>
  <url><loc>https://www.rillet.com/case-studies/alpha</loc></url>
  <url><loc>https://www.rillet.com/blog/continuous-close</loc></url>
  <url><loc>https://www.rillet.com/compare/netsuite</loc></url>
  <url><loc>https://www.rillet.com/solutions/private-equity</loc></url>
  <url><loc>https://www.rillet.com/case-studies/beta</loc></url>
  <url><loc>https://www.rillet.com/careers</loc></url>
  <url><loc>https://evil.example.com/case-studies/hijack</loc></url>
</urlset>"""

# Sorted pattern hits — the deterministic rotation order.
EXPECTED_SORTED = [
    "https://www.rillet.com/blog/continuous-close",
    "https://www.rillet.com/case-studies/alpha",
    "https://www.rillet.com/case-studies/beta",
    "https://www.rillet.com/compare/netsuite",
    "https://www.rillet.com/solutions/private-equity",
]

ACCOUNT = Account(domain=DOMAIN, name="Rillet")


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


def _ctx(tmp_path, *, content_type="application/rss+xml; charset=utf-8", body=SITEMAP_XML):
    """tmp Database + RawStore with the sitemap document stored (200)."""
    db = Database(tmp_path / "signals.db")
    store = RawStore(db, tmp_path / "raw")
    if body is not None:
        store.put(
            source="ats_discovery",
            url=SITEMAP_URL,
            body=body.encode("utf-8"),
            content_type=content_type,
            status=200,
            domain=DOMAIN,
        )
    return db, store


def _store_page(store, url, body, *, content_type="text/html; charset=utf-8"):
    return store.put(
        source="sitecontent",
        url=url,
        body=body,
        content_type=content_type,
        status=200,
        domain=DOMAIN,
    )


# --------------------------------------------------------------------------
# pure helpers
# --------------------------------------------------------------------------


def test_extract_locs_regex_and_dedupe():
    xml = (
        "<urlset><url><loc>https://a.com/x</loc></url>"
        "<url><loc>  https://a.com/y  </loc></url>"
        "<url><loc>https://a.com/x</loc></url>"
        "<url><loc>https://a.com/amp&amp;more</loc></url></urlset>"
    )
    assert extract_locs(xml) == [
        "https://a.com/x",
        "https://a.com/y",
        "https://a.com/amp&more",
    ]


def test_filter_patterns_casefold_and_default_order():
    urls = [
        "https://www.rillet.com/Blog/Post",
        "https://www.rillet.com/pricing",
        "https://www.rillet.com/compare/netsuite",
    ]
    assert filter_patterns(urls, ["/blog/", "/compare/"]) == [
        "https://www.rillet.com/Blog/Post",
        "https://www.rillet.com/compare/netsuite",
    ]


def test_page_prose_title_and_cap():
    html = "<html><head><title>Acme — Case Study</title></head><body><p>hello world</p></body></html>"
    out = page_prose(html, max_chars=50_000)
    assert out["title"] == "Acme — Case Study"
    assert "hello world" in out["text"]
    assert page_prose("", max_chars=100) == {"title": None, "text": ""}


# --------------------------------------------------------------------------
# plan(): extraction, filtering, same-site guard, skip, cap, rotation
# --------------------------------------------------------------------------


def test_plan_extracts_filters_and_keeps_same_site_only(tmp_path):
    db, store = _ctx(tmp_path)
    tasks = SiteContentSource().plan(ACCOUNT, None, db=db, store=store)
    urls = [t.url for t in tasks]
    assert urls == EXPECTED_SORTED  # pattern filter + sorted + dedupe
    assert "https://www.rillet.com/pricing" not in urls  # not a content pattern
    assert "https://www.rillet.com/careers" not in urls
    assert all(t.source == "sitecontent" for t in tasks)
    assert all(t.domain == DOMAIN for t in tasks)
    assert all(t.meta.get("kind") == "page" for t in tasks)


def test_plan_requires_a_stored_sitemap_document(tmp_path):
    db, store = _ctx(tmp_path, body=None)
    assert SiteContentSource().plan(ACCOUNT, None, db=db, store=store) == []


def test_plan_ignores_docs_that_do_not_sniff_as_sitemap(tmp_path):
    db, store = _ctx(tmp_path, body=None)
    # An HTML page stored under a sitemap-looking URL must NOT plan (sniff decides).
    store.put(
        source="ats_discovery",
        url=SITEMAP_URL,
        body=b"<html><body>404 shell</body></html>",
        content_type="text/html; charset=utf-8",
        status=200,
        domain=DOMAIN,
    )
    assert SiteContentSource().plan(ACCOUNT, None, db=db, store=store) == []


def test_plan_skips_already_fetched_urls(tmp_path):
    db, store = _ctx(tmp_path)
    _store_page(store, EXPECTED_SORTED[1], b"<html>x</html>")
    urls = [t.url for t in SiteContentSource().plan(ACCOUNT, None, db=db, store=store)]
    assert EXPECTED_SORTED[1] not in urls
    assert len(urls) == len(EXPECTED_SORTED) - 1


def test_plan_skips_trailing_slash_variants(tmp_path):
    db, store = _ctx(tmp_path)
    _store_page(store, EXPECTED_SORTED[0] + "/", b"<html>x</html>")
    urls = [t.url for t in SiteContentSource().plan(ACCOUNT, None, db=db, store=store)]
    assert EXPECTED_SORTED[0] not in urls


def test_plan_caps_per_cycle_and_rotates_to_cover_the_rest(tmp_path, monkeypatch):
    import src.sources.sitecontent.collector as mod

    monkeypatch.setattr(
        mod, "load_sitecontent_cfg", lambda: {**mod.DEFAULT_CFG, "max_fetches_per_cycle": 2}
    )
    db, store = _ctx(tmp_path)
    src = SiteContentSource()

    seen: list[str] = []
    cursor = None
    for _ in range(10):
        tasks = src.plan(ACCOUNT, cursor, db=db, store=store)
        assert len(tasks) <= 2
        if not tasks:
            break
        for t in tasks:
            seen.append(t.url)
            # The runner stores every fetched body in the raw store (a
            # documents row keyed on it) — plan() must skip it next cycle.
            # Distinct bodies: the store is content-addressed, identical
            # bytes would collapse into ONE documents row.
            _store_page(store, t.url, b"<html>page " + t.url.encode() + b"</html>")
        # The runner stores next_cursor(last_doc, cands); the last fetched doc
        # is the cycle's last planned URL.
        cursor = src.next_cursor(
            Document(doc_id="d", source="sitecontent", url=seen[-1]), []
        )
    # Full coverage, no repeats, then the adapter plans nothing while the
    # sitemap yields nothing new.
    assert sorted(seen) == EXPECTED_SORTED
    assert len(seen) == len(set(seen))
    assert src.plan(ACCOUNT, cursor, db=db, store=store) == []


def test_plan_unknown_cursor_starts_from_head(tmp_path):
    db, store = _ctx(tmp_path)
    tasks = SiteContentSource().plan(ACCOUNT, "https://www.rillet.com/gone", db=db, store=store)
    assert [t.url for t in tasks] == EXPECTED_SORTED


# --------------------------------------------------------------------------
# parse(): prose floor + thin marker
# --------------------------------------------------------------------------


def _page_html(n_chars=600, title="Case Study: Alpha"):
    prose = ("Rillet replaced their legacy close with a continuous close. " * 20)[:n_chars]
    return f"<html><head><title>{title}</title></head><body><p>{prose}</p></body></html>"


def test_parse_emits_content_appearance_marker_above_floor(tmp_path):
    db, store = _ctx(tmp_path)
    url = EXPECTED_SORTED[1]
    _store_page(store, url, _page_html().encode("utf-8"))
    doc = store.get(_doc_id_for(store, url))
    cands = SiteContentSource().parse(doc, ACCOUNT, {"today": TODAY})
    assert len(cands) == 1
    c = cands[0]
    assert c.signal_type == "content_appearance"
    assert c.observed_at == TODAY
    assert c.url == url
    assert c.title == "Case Study: Alpha"
    assert c.evidence_data["chars"] >= 500
    assert c.evidence_data["doc_id"] == doc.doc_id


def _doc_id_for(store, url):
    row = store.db.one("SELECT doc_id FROM documents WHERE url=? AND domain=?", (url, DOMAIN))
    assert row, url
    return row["doc_id"]


def test_parse_below_floor_emits_nothing(tmp_path):
    db, store = _ctx(tmp_path)
    url = EXPECTED_SORTED[1]
    _store_page(store, url, b"<html><body><p>tiny</p></body></html>")
    doc = store.get(_doc_id_for(store, url))
    assert SiteContentSource().parse(doc, ACCOUNT, {"today": TODAY}) == []


def test_parse_caps_prose_at_max_chars(tmp_path, monkeypatch):
    import src.sources.sitecontent.collector as mod

    monkeypatch.setattr(
        mod, "load_sitecontent_cfg", lambda: {**mod.DEFAULT_CFG, "max_prose_chars": 512}
    )
    db, store = _ctx(tmp_path)
    url = EXPECTED_SORTED[1]
    _store_page(store, url, _page_html(n_chars=5000).encode("utf-8"))
    doc = store.get(_doc_id_for(store, url))
    cands = SiteContentSource().parse(doc, ACCOUNT, {"today": TODAY})
    assert cands and cands[0].evidence_data["chars"] == 512


def test_parse_natural_key_is_idempotent_per_url(tmp_path):
    db, store = _ctx(tmp_path)
    url = EXPECTED_SORTED[1]
    _store_page(store, url, _page_html().encode("utf-8"))
    doc = store.get(_doc_id_for(store, url))
    src = SiteContentSource()
    a = src.parse(doc, ACCOUNT, {"today": TODAY})
    b = src.parse(doc, ACCOUNT, {"today": "2026-11-06"})
    assert a[0].natural_key == b[0].natural_key


def test_parse_never_guesses_observed_at(tmp_path):
    db, store = _ctx(tmp_path)
    url = EXPECTED_SORTED[1]
    _store_page(store, url, _page_html().encode("utf-8"))
    doc = store.get(_doc_id_for(store, url))
    doc.fetched_at = None
    # No today in meta and no fetched_at: no date to cite — emit nothing.
    assert SiteContentSource().parse(doc, ACCOUNT, {}) == []


def test_parse_empty_body_emits_nothing():
    doc = Document(doc_id="x", source="sitecontent", url=EXPECTED_SORTED[1], body=None)
    assert SiteContentSource().parse(doc, ACCOUNT, {"today": TODAY}) == []


# --------------------------------------------------------------------------
# config loader + registration
# --------------------------------------------------------------------------


def test_cfg_defaults_when_yaml_missing(tmp_path, monkeypatch):
    import src.sources.sitecontent.config as cfgmod

    monkeypatch.setattr(cfgmod, "SOURCES_YAML_PATH", tmp_path / "missing.yaml")
    cfg = cfgmod.load_sitecontent_cfg()
    assert cfg["content_patterns"] == [
        "/case-studies/",
        "/blog/",
        "/compare/",
        "/solutions/",
    ]
    assert cfg["max_fetches_per_cycle"] == 20
    assert cfg["prose_floor_chars"] == 500
    assert cfg["max_prose_chars"] == 50000


def test_sitecontent_registered_and_enabled():
    import src.sources  # noqa: F401 — @register only populates when imported
    from src.sources.registry import SOURCES

    assert SOURCES["sitecontent"] is SiteContentSource

    from src.core.config import Config
    from src.sources.registry import enabled_sources

    cfg = Config()
    cfg.config_dir = "config"
    keys = [a.key for a in enabled_sources(cfg)]
    assert "sitecontent" in keys


def test_rss_content_type_still_sniffs_as_sitemap(tmp_path):
    """The spike's measured quirk: the stored sitemap ships as rss+xml content
    type — the BODY sniff (not the content type) must classify it."""
    db, store = _ctx(tmp_path)  # _ctx stores with application/rss+xml
    tasks = SiteContentSource().plan(ACCOUNT, None, db=db, store=store)
    assert [t.url for t in tasks] == EXPECTED_SORTED


def test_raw_bytes_path(tmp_path):
    """Round-trip: gzip-stored body bytes flow through the same helpers."""
    blob = gzip.compress(SITEMAP_XML.encode("utf-8"))
    assert extract_locs(gzip.decompress(blob).decode("utf-8"))[:1] == [
        "https://www.rillet.com/"
    ]
