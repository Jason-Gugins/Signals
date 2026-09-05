"""Task 1 (roadmap 2026-09-04): Wayback homepage positioning diff.

Covers the pure ``diff_positioning`` extractor/normalizer rules, the wayback
``parse`` wiring for ``kind == "snapshot"``, the ``follow_tasks`` kind guard
(snapshot docs carry HTML — feeding them to parse_cdx aborts the pass), the
runner's ``prev_homepage_html`` injection (mirror of the proven
``prev_pricing_html`` mechanism), and a persistence test pinning that
``positioning_change`` survives ``normalize_batch`` (taxonomy entry was
pre-registered in 5794876).
"""

from __future__ import annotations

import json

from src.core.models import Account
from src.signals.normalize import normalize_batch
from src.signals.taxonomy import Taxonomy
from src.sources.wayback.collector import WaybackSource
from src.sources.wayback.positioning import diff_positioning

PREV_HTML = (
    "<html><head><title>Acme — Project Management for Teams</title>"
    '<meta name="description" content="Simple task tracking for small teams.">'
    "</head><body><h1>Acme</h1></body></html>"
)
CURR_HTML = (
    "<html><head><title>Acme — The AI Workspace</title>"
    '<meta name="description" content="Enterprise AI workflows for every team.">'
    "</head><body><h1>Acme</h1></body></html>"
)


# ── pure diff_positioning ────────────────────────────────────────────────────


def test_description_change_emits_candidate():
    # The runner injects the prev html as str and doc.body is bytes — accept
    # the exact mixed shapes the caller produces.
    cand = diff_positioning(
        PREV_HTML, CURR_HTML.encode("utf-8"), domain="acme.com", today="2026-09-04"
    )
    assert cand is not None
    assert cand.signal_type == "positioning_change"
    assert cand.natural_key == "poschg:acme.com:2026-09-04"
    assert cand.observed_at == "2026-09-04"
    assert cand.confidence == 0.6
    ev = cand.evidence_data
    assert "Simple task tracking" in ev["old_description"]
    assert "Enterprise AI workflows" in ev["new_description"]
    assert ev["old_title"] == "Acme — Project Management for Teams"
    assert ev["new_title"] == "Acme — The AI Workspace"


def test_identical_pages_yield_none():
    assert diff_positioning(PREV_HTML, PREV_HTML, domain="acme.com", today="2026-09-04") is None


def test_case_and_whitespace_only_changes_yield_none():
    """Values are normalized (casefold + whitespace collapse) BEFORE compare."""
    noisy = (
        "<html><head><title>  acme   — PROJECT management FOR teams </title>"
        "<meta name='description' content='simple   TASK tracking for small TEAMS.'>"
        "</head></html>"
    )
    assert diff_positioning(PREV_HTML, noisy, domain="acme.com", today="2026-09-04") is None


def test_empty_prev_yields_none():
    """Missing prev or a prev without any title/description -> never guess."""
    assert diff_positioning(b"", CURR_HTML, domain="acme.com", today="2026-09-04") is None
    assert (
        diff_positioning("<html><body>no meta here</body></html>", CURR_HTML, domain="acme.com", today="2026-09-04")
        is None
    )


def test_empty_curr_yields_none():
    assert diff_positioning(PREV_HTML, b"", domain="acme.com", today="2026-09-04") is None


def test_title_only_change_emits_candidate():
    curr = (
        "<html><head><title>Acme Reborn</title>"
        '<meta name="description" content="Simple task tracking for small teams.">'
        "</head></html>"
    )
    cand = diff_positioning(PREV_HTML, curr, domain="acme.com", today="2026-09-04")
    assert cand is not None
    assert cand.title == "Acme Reborn"
    # Description unchanged on both sides — only the title flipped.
    assert (
        cand.evidence_data["old_description"] == cand.evidence_data["new_description"]
        == "Simple task tracking for small teams."
    )


def test_og_description_used_as_fallback():
    """Pages without <meta name=description> diff via og:description."""
    prev = (
        '<html><head><title>Acme</title>'
        '<meta property="og:description" content="Old positioning lives here"></head></html>'
    )
    curr = (
        '<html><head><title>Acme</title>'
        '<meta property="og:description" content="New positioning moved there"></head></html>'
    )
    cand = diff_positioning(prev, curr, domain="acme.com", today="2026-09-04")
    assert cand is not None
    assert cand.evidence_data["old_description"] == "Old positioning lives here"
    assert cand.evidence_data["new_description"] == "New positioning moved there"


def test_candidate_title_falls_back_to_description_snippet():
    """No title on either side + changed description: the candidate title is
    the first 60 chars of the new description."""
    long_desc = "A" * 80
    prev = '<html><head><meta name="description" content="B"></head></html>'
    curr = f'<html><head><meta name="description" content="{long_desc}"></head></html>'
    cand = diff_positioning(prev, curr, domain="acme.com", today="2026-09-04")
    assert cand is not None
    assert cand.title == "A" * 60


# ── collector wiring (kind == "snapshot") ───────────────────────────────────


def _doc(body: bytes):
    return type("D", (), {"body": body})()


def test_parse_snapshot_wiring():
    src = WaybackSource()
    acct = Account(domain="acme.com")
    meta = {"kind": "snapshot", "prev_homepage_html": PREV_HTML, "today": "2026-09-04"}
    cands = src.parse(_doc(CURR_HTML.encode("utf-8")), acct, meta)
    assert len(cands) == 1
    assert cands[0].signal_type == "positioning_change"

    # Conservative contract: no prev_homepage_html -> no signal (never guess).
    assert src.parse(_doc(CURR_HTML.encode("utf-8")), acct, {"kind": "snapshot", "today": "2026-09-04"}) == []
    assert src.parse(_doc(CURR_HTML.encode("utf-8")), acct, {"kind": "snapshot"}) == []


def test_parse_pricing_branch_still_works():
    """The snapshot branch must not disturb the proven pricing branch."""
    src = WaybackSource()
    acct = Account(domain="acme.com")
    prev = "<html><div class='pricing'><p>Basic $10</p></div></html>"
    curr = "<html><div class='pricing'><p>Basic $49</p></div></html>"
    cands = src.parse(
        _doc(curr.encode("utf-8")),
        acct,
        {"kind": "pricing", "prev_pricing_html": prev, "today": "2026-09-04"},
    )
    assert len(cands) == 1
    assert cands[0].signal_type == "pricing_change"


def test_follow_tasks_skips_html_snapshot_docs():
    """Snapshot/pricing docs carry HTML; follow_tasks must not re-parse them
    as CDX JSON (JSONDecodeError aborted the whole runner pass and dropped
    every candidate parsed in it, including the new positioning signal)."""
    src = WaybackSource()
    acct = Account(domain="acme.com")
    html_doc = _doc(b"<html><title>Acme</title></html>")
    assert src.follow_tasks(html_doc, acct, {"kind": "snapshot"}) == []
    assert src.follow_tasks(html_doc, acct, {"kind": "pricing"}) == []

    # CDX-kind docs (and legacy no-kind direct calls) still produce follows.
    cdx = json.dumps([["timestamp", "original"], ["20240101000000", "http://acme.com/"]]).encode("utf-8")
    follows = src.follow_tasks(_doc(cdx), acct, {"kind": "cdx"})
    assert [t for t in follows if (t.meta or {}).get("kind") == "snapshot"]


# ── persistence (taxonomy survival, the tech_churn house rule) ──────────────


def test_positioning_change_persists_through_normalize():
    cand = diff_positioning(PREV_HTML, CURR_HTML, domain="acme.com", today="2026-09-04")
    assert cand is not None
    valid, rejected = normalize_batch(
        [cand],
        account=Account(domain="acme.com", name="Acme"),
        source="wayback",
        taxonomy=Taxonomy.load(),
        now="2026-09-04T00:00:00Z",
    )
    assert len(valid) == 1
    assert rejected == []
    assert valid[0].signal_type == "positioning_change"


# ── runner prev_homepage_html injection (end-to-end, real adapter) ──────────


def test_runner_injects_prev_homepage_html(tmp_path):
    """END-TO-END: the runner must inject the previously stored homepage
    snapshot as prev_homepage_html so positioning_change actually persists
    through the real WaybackSource flow (plan -> cdx -> snapshot follow)."""
    from src.core.config import Config
    from src.core.db import Database
    from src.core.http import FetchResult
    from src.core.rawstore import RawStore
    from src.core.runlog import RunContext
    from src.identity.registry import AccountRegistry
    from src.pipeline.runner import CollectorRunner
    from src.signals.store import SignalStore

    CDX = json.dumps([["timestamp", "original"], ["20240101000000", "http://acme.com/"]]).encode("utf-8")

    class _Fetch:
        def __init__(self, store):
            self.store = store

        def get(self, task, *, etag=None, last_modified=None):
            if (task.meta or {}).get("kind") == "cdx":
                body = CDX
            else:
                body = CURR_HTML.encode("utf-8")
            doc = self.store.put(
                source=task.source, url=task.url, body=body,
                content_type="text/html", status=200, domain=task.domain,
            )
            return FetchResult(True, 200, doc, False, None, 1)

    db = Database(tmp_path / "s.db")
    cfg = Config()
    cfg.http.max_workers = 1
    cfg.http.respect_robots = False
    store = RawStore(db, tmp_path / "raw")
    tax = Taxonomy.load()
    ctx = RunContext(db, "collect")
    ctx.__enter__()
    # A PRIOR homepage snapshot for this domain already stored (older fetch).
    store.put(
        source="wayback",
        url="https://web.archive.org/web/20230101000000id_/http://acme.com/",
        body=PREV_HTML.encode("utf-8"),
        content_type="text/html",
        status=200,
        domain="acme.com",
        fetched_at="2026-08-01T00:00:00+00:00",
    )
    runner = CollectorRunner(
        cfg, db, AccountRegistry(db), store, _Fetch(store), SignalStore(db, tax), tax, ctx,
    )
    try:
        stats = runner.run([WaybackSource()], [Account(domain="acme.com")], force=True)
    finally:
        ctx.__exit__(None, None, None)

    row = db.one("SELECT COUNT(*) AS n FROM signals WHERE signal_type='positioning_change'")
    assert row["n"] == 1, (
        "positioning_change never persisted — prev_homepage_html injection "
        "or the snapshot parse wiring is broken"
    )
    sig = db.one("SELECT source, title FROM signals WHERE signal_type='positioning_change'")
    assert sig["source"] == "wayback"
    assert stats.by_source["wayback"]["signals_new"] == 1
