"""Empty-since bookkeeping parity: capterra/trustradius like the G2 path.

``_filter_backoff`` applies empty-since backoff to EVERY ``marketplace_*``
source, but ``record_empty``/``record_reviews`` fired only inside the G2
fragment path (``_fetch_g2_fragment``) — capterra/trustradius slugs could
never enter or reset backoff (the mechanism was half-wired). These tests
drive the runner end-to-end with marketplace-shaped stub adapters and pin
the generalized bookkeeping.

Shared JSON state (empty log, marketplace trend stats) is pointed at
tmp_path — never at data/.
"""

from __future__ import annotations

from src.core.config import Config
from src.core.db import Database
from src.core.http import FetchResult
from src.core.models import Account, Document
from src.core.rawstore import RawStore
from src.core.runlog import RunContext
from src.identity.registry import AccountRegistry
from src.pipeline.runner import CollectorRunner
from src.signals.store import SignalStore
from src.signals.taxonomy import Taxonomy
from src.sources.base import FetchTask, SourceAdapter
from src.sources.marketplace.empty_log import EmptyLog

SLUG = "19319/JIRA"
CAPTERRA_URL = f"https://www.capterra.com/p/{SLUG}/reviews/"
TRUSTRADIUS_URL = "https://www.trustradius.com/products/jira/reviews"


class _StubFetch:
    def __init__(self, body: bytes = b"<html><body>reviews page</body></html>"):
        self.body = body
        self.calls: list[str] = []

    def get(self, task, *, etag=None, last_modified=None):
        self.calls.append(task.url)
        doc = Document(
            doc_id=f"d{len(self.calls)}", source=task.source, url=task.url,
            domain=task.domain, body=self.body, status=200,
        )
        return FetchResult(True, 200, doc, False, None, 1)


class _MarketplaceStub(SourceAdapter):
    """Duck-typed marketplace adapter: parse emits nothing; harvest_reviews
    returns the configured list (empty list = zero rendered reviews)."""

    tier = "http"
    cadence_hours = 24

    def __init__(self, key: str, url: str, reviews: list | None = None):
        self.key = key
        self._url = url
        self._reviews = reviews if reviews is not None else []

    def plan(self, account, cursor):
        return [
            FetchTask(
                source=self.key, url=self._url, domain=account.domain,
                meta={"kind": "reviews", "product_slug": SLUG, "page": 1},
            )
        ]

    def parse(self, doc, account, task_meta):
        return []

    def harvest_reviews(self, doc, account, task_meta):
        return self._reviews


def _account() -> Account:
    return Account(domain="jira.atlassian.com", name="Jira")


def _make_runner(tmp_path, monkeypatch, fetch: _StubFetch):
    """Harness mirroring tests/test_runner.py, with the shared JSON state
    (empty log + marketplace trend stats) redirected into tmp_path."""
    import src.sources.marketplace.trend as trend_mod

    stats_path = tmp_path / "stats.json"
    real_load, real_save = trend_mod.load_stats, trend_mod.save_stats
    monkeypatch.setattr(trend_mod, "DEFAULT_STATS_PATH", str(stats_path))
    monkeypatch.setattr(trend_mod, "load_stats", lambda *a, **k: real_load(stats_path))
    monkeypatch.setattr(
        trend_mod, "save_stats", lambda st, *a, **k: real_save(st, stats_path)
    )

    db = Database(tmp_path / "s.db")
    cfg = Config()
    cfg.http.max_workers = 1
    cfg.http.respect_robots = False
    store = RawStore(db, tmp_path / "raw")
    tax = Taxonomy.load()
    ctx = RunContext(db, "collect")
    ctx.__enter__()
    runner = CollectorRunner(
        cfg, db, AccountRegistry(db), store, fetch, SignalStore(db, tax), tax, ctx
    )
    runner._empty_log = EmptyLog(tmp_path / "empty_slugs.json")
    return runner, db, ctx


def test_capterra_zero_reviews_records_empty(tmp_path, monkeypatch):
    """A capterra account whose page renders zero reviews records an empty
    under the CAPTERRA source key (previously only G2 fed the log)."""
    fetch = _StubFetch()
    adapter = _MarketplaceStub("marketplace_capterra", CAPTERRA_URL)
    runner, _db, ctx = _make_runner(tmp_path, monkeypatch, fetch)
    try:
        runner.run([adapter], [_account()], force=True)
    finally:
        ctx.__exit__(None, None, None)

    entry = runner.empty_log.entry(SLUG, "capterra")
    assert entry.get("cycles") == 1
    assert entry.get("first_empty")
    # ...and NOT under the g2 key (per-source keys must not bleed)
    assert runner.empty_log.entry(SLUG, "g2") == {}


def test_capterra_reviews_upserted_and_empty_streak_reset(tmp_path, monkeypatch):
    """A capterra account WITH reviews: the review lands in g2_reviews and
    any pre-existing empty streak for that slug is reset."""
    from src.sources.marketplace.capterra import CapterraReview

    fetch = _StubFetch()
    adapter = _MarketplaceStub(
        "marketplace_capterra",
        CAPTERRA_URL,
        reviews=[
            CapterraReview(
                review_id="cap-1", product_slug=SLUG, reviewer_name="Jane Doe",
                rating=5.0, posted_at="2026-08-30", review_title="Great",
                review_body="Solid tool",
            )
        ],
    )
    runner, db, ctx = _make_runner(tmp_path, monkeypatch, fetch)
    log = runner.empty_log
    log.record_empty(SLUG, "capterra")
    log.record_empty(SLUG, "capterra")
    try:
        runner.run([adapter], [_account()], force=True)
    finally:
        ctx.__exit__(None, None, None)

    # empty streak reset by the review-bearing cycle
    assert log.entry(SLUG, "capterra") == {}
    # the review was upserted with capterra provenance
    row = db.one("SELECT source FROM g2_reviews WHERE review_id='cap-1'")
    assert row is not None
    assert row["source"] == "capterra"


def test_trustradius_zero_reviews_records_empty(tmp_path, monkeypatch):
    """Same parity for trustradius: its empties are keyed under
    'trustradius:<slug>', which is what _filter_backoff consults."""
    fetch = _StubFetch()
    adapter = _MarketplaceStub("marketplace_trustradius", TRUSTRADIUS_URL)
    runner, _db, ctx = _make_runner(tmp_path, monkeypatch, fetch)
    try:
        runner.run([adapter], [_account()], force=True)
    finally:
        ctx.__exit__(None, None, None)

    entry = runner.empty_log.entry(SLUG, "trustradius")
    assert entry.get("cycles") == 1
    assert runner.empty_log.entry(SLUG, "g2") == {}


def test_zero_review_slug_enters_backoff_after_threshold(tmp_path, monkeypatch):
    """Three consecutive empty capterra cycles put the slug into backoff and
    the runner stops fetching it — the half-wired mechanism now completes."""
    fetch = _StubFetch()
    adapter = _MarketplaceStub("marketplace_capterra", CAPTERRA_URL)
    runner, _db, ctx = _make_runner(tmp_path, monkeypatch, fetch)
    log = runner.empty_log
    log.record_empty(SLUG, "capterra")
    log.record_empty(SLUG, "capterra")
    try:
        runner.run([adapter], [_account()], force=True)  # 3rd consecutive empty
        assert log.is_in_backoff(SLUG, "capterra")
        fetches_after_third = len(fetch.calls)
        runner.run([adapter], [_account()], force=True)  # must be skipped
        assert len(fetch.calls) == fetches_after_third
    finally:
        ctx.__exit__(None, None, None)
