from datetime import date
from pathlib import Path
from src.sources.techstack.fingerprint import TechMatch
from src.sources.wayback.cdx import parse_cdx, pick_snapshots, snapshot_url
from src.sources.wayback.renewal import estimate_first_seen, renewal_candidates


def test_cdx_and_pick():
    rows = parse_cdx((Path("tests/fixtures/wayback/cdx.json")).read_bytes())
    assert rows[0]["timestamp"].startswith("2020")
    picked = pick_snapshots(rows, per_year=1, max_total=3)
    assert len(picked) <= 3
    assert snapshot_url("20200101120000", "http://acme.com/").startswith("https://web.archive.org/web/20200101120000id_/")


def test_estimate_and_renewal_window():
    dated = [
        ("2024-08-01", [TechMatch("hubspot", "HubSpot", ["crm"], "mid", "s", 0.8)]),
        ("2025-08-01", [TechMatch("hubspot", "HubSpot", ["crm"], "mid", "s", 0.8)]),
    ]
    assert estimate_first_seen("hubspot", dated) == "2024-08-01"
    assert estimate_first_seen("nope", dated) is None
    today = date(2026, 7, 15)
    rows = [{"vendor": "hubspot", "first_seen_at": "2025-08-20"}]
    cands = renewal_candidates("acme.com", rows, contract_years={}, default_years=1, today=today)
    assert cands and cands[0].signal_type == "renewal_window"
    assert renewal_candidates("acme.com", [{"vendor": "x"}], contract_years={}, today=today) == []


# ── Task 17: pricing follow-task + prev-snapshot injection regression ─────


def test_pricing_follow_skips_when_no_pricing_row(tmp_path):
    """A CDX listing with no /pricing URL must not fabricate a pricing task."""
    import json as _json
    from src.sources.wayback.collector import WaybackSource
    from src.core.models import Account

    # CDX JSON shape: [header, row, row...]
    raw = [
        ["timestamp", "original"],
        ["20200101120000", "https://acme.com/"],
        ["20210101120000", "https://acme.com/about"],
    ]
    doc = type("D", (), {"body": _json.dumps(raw).encode("utf-8")})()
    follows = WaybackSource().follow_tasks(doc, Account(domain="acme.com"), {})
    assert not [t for t in follows if (t.meta or {}).get("kind") == "pricing"]


def test_runner_injects_prev_pricing_html(tmp_path, monkeypatch):
    """END-TO-END regression for the review blocker: the runner must inject
    prev_pricing_html from the prior stored pricing snapshot so pricing_change
    can actually fire (previously parse() always returned [])."""
    import sys, tempfile
    from src.core.config import Config
    from src.core.db import Database
    from src.core.models import Account
    from src.core.rawstore import RawStore
    from src.core.runlog import RunContext
    from src.identity.registry import AccountRegistry
    from src.pipeline.runner import CollectorRunner
    from src.signals.store import SignalStore
    from src.signals.taxonomy import Taxonomy
    from src.sources.base import FetchTask, SourceAdapter
    from src.sources.ats.common import JobPost  # noqa: F401 (import surface)

    PREV = b"<html><div class='pricing'><p>Basic $10</p><p>Pro $25</p></div></html>"
    CURR = b"<html><div class='pricing'><p>Basic $10</p><p>Pro $49</p></div></html>"

    class _FakeStore:
        """RawStore stand-in serving the previously stored pricing doc."""

        def __init__(self, db, prev_doc_id):
            self.db = db
            self.prev_doc_id = prev_doc_id

        def put(self, **kw):
            db.execute(
                """INSERT OR REPLACE INTO documents
                   (doc_id, source, domain, url, status, fetched_at)
                   VALUES (?, 'wayback', ?, ?, 200, ?)""",
                (kw.get("doc_id"), kw.get("domain"), kw.get("url"), kw.get("fetched_at")),
            )
            return type("D", (), {"doc_id": kw.get("doc_id")})()

        def get(self, doc_id):
            if doc_id != self.prev_doc_id:
                return None
            return type("D", (), {"body": PREV})()

    class _PricingFetch:
        def get(self, task, *, etag=None, last_modified=None):
            from src.core.http import FetchResult

            doc = type(
                "D", (), {"doc_id": "curr-doc", "source": task.source, "url": task.url,
                          "domain": task.domain, "body": CURR, "status": 200},
            )()
            return FetchResult(True, 200, doc, False, None, 1)

    db = Database(tmp_path / "s.db")
    # A PRIOR pricing snapshot for this domain already exists in documents.
    db.execute(
        """INSERT INTO documents (doc_id, source, domain, url, status, fetched_at)
           VALUES ('prev-doc', 'wayback', 'acme.com', 'https://acme.com/pricing', 200, '2026-08-01T00:00:00Z')"""
    )
    cfg = Config()
    cfg.http.max_workers = 1
    cfg.http.respect_robots = False
    tax = Taxonomy.load()
    ctx = RunContext(db, "collect")
    ctx.__enter__()
    runner = CollectorRunner(
        cfg, db, AccountRegistry(db), _FakeStore(db, "prev-doc"), _PricingFetch(),
        SignalStore(db, tax), tax, ctx,
    )

    captured: dict = {}

    class _Wayback(SourceAdapter):
        key = "wayback"
        tier = "http"
        cadence_hours = 24

        def plan(self, account, cursor):
            return [FetchTask(source=self.key, url="https://web.archive.org/pricing",
                              domain=account.domain, meta={"kind": "pricing"})]

        def parse(self, doc, account, task_meta):
            captured["prev"] = (task_meta or {}).get("prev_pricing_html")
            from src.sources.wayback.pricing import diff_pricing
            cand = diff_pricing(
                captured["prev"] or "", doc.body, domain=account.domain,
                today=(task_meta or {}).get("today"),
            )
            return [cand] if cand is not None else []

    runner.run([_Wayback()], [Account(domain="acme.com", ats_vendor="smartrecruiters", ats_token="t")])
    ctx.__exit__(None, None, None)

    # The runner must have injected the PREVIOUS snapshot's html.
    assert captured["prev"] == PREV.decode("utf-8")
    # And the pricing_change signal must have been persisted.
    row = db.one("SELECT COUNT(*) AS n FROM signals WHERE signal_type='pricing_change'")
    assert row["n"] == 1
