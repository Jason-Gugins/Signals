"""Task 2 (roadmap 2026-09-04): appstore review-trend wiring + version capture.

Three defects pinned here:

(a) The runner's review-trend filing never fired for appstore_reviews and
    would have garbled the stats key: ``source_name =
    adapter.key[len("marketplace_"):]`` slices "appstore_reviews" into
    "iews". Driven end-to-end with an appstore-shaped stub adapter (plain
    dict reviews, like the real harvest) exactly the way
    test_marketplace_empty_parity drives marketplace stubs: the stats file
    must gain ``appstore_reviews:<app_id>`` — NOT ``iews:<app_id>``.
(b) ``plan()`` meta carries ``product_slug`` (the app id IS the stable
    per-app slug, same role as marketplace slugs) — without it the runner's
    ``review_harvests`` filing (slug = meta["product_slug"]) never fires.
(c) ``parse_itunes_reviews`` captures the feed's ``im:version`` into the
    review dict. No schema migration by design: g2_reviews has no version
    column, so column persistence is deliberately skipped — the value rides
    in the review dict (trend/evidence path) and the upsert must tolerate it.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.core.db import Database
from src.core.models import Account
from src.sources.base import SourceAdapter

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "appstores"
APP_ID = "618783545"


# ── (b) plan() meta carries product_slug ─────────────────────────────────────


def test_appstore_plan_meta_carries_product_slug():
    from src.sources.appstores.appstore import AppStoreReviewSource

    acct = Account(domain="acme.com", app_store_id=APP_ID)
    tasks = AppStoreReviewSource().plan(acct, cursor=None)
    assert len(tasks) == 1
    assert tasks[0].meta["kind"] == "reviews"
    assert tasks[0].meta["app_store_id"] == APP_ID
    # The app id IS the per-app slug: review_harvests filing keys on it.
    assert tasks[0].meta["product_slug"] == APP_ID


def test_appstore_plan_meta_slug_per_id_for_multi_id_accounts():
    from src.sources.appstores.appstore import AppStoreReviewSource

    acct = Account(domain="acme.com", app_store_id=f"{APP_ID}, 1234567")
    tasks = AppStoreReviewSource().plan(acct, cursor=None)
    assert [t.meta["product_slug"] for t in tasks] == [APP_ID, "1234567"]
    assert [t.meta["app_store_id"] for t in tasks] == [APP_ID, "1234567"]


# ── (a) runner files review-trend stats under appstore_reviews ──────────────


class _StubFetch:
    def get(self, task, *, etag=None, last_modified=None):
        from src.core.http import FetchResult

        doc = type(
            "D", (), {
                "doc_id": "d1", "source": task.source, "url": task.url,
                "domain": task.domain, "body": b"reviews feed", "status": 200,
            },
        )()
        return FetchResult(True, 200, doc, False, None, 1)


class _AppstoreStub(SourceAdapter):
    """Appstore-shaped stub adapter: plain-dict reviews like the real
    harvest_reviews yield (NOT attribute objects like marketplace sources).
    """

    key = "appstore_reviews"
    tier = "http"
    cadence_hours = 168
    requires = ()

    def __init__(self, reviews: list[dict]):
        self._reviews = reviews

    def plan(self, account, cursor):
        from src.sources.base import FetchTask

        return [
            FetchTask(
                source=self.key,
                url=f"https://itunes.apple.com/us/rss/customerreviews/id={APP_ID}/sortby=mostrecent/json",
                domain=account.domain,
                meta={"kind": "reviews", "app_store_id": APP_ID, "product_slug": APP_ID},
            )
        ]

    def parse(self, doc, account, task_meta):
        return []

    def harvest_reviews(self, doc, account, task_meta):
        return self._reviews


def _make_runner(tmp_path, monkeypatch):
    """Harness mirroring test_marketplace_empty_parity: shared trend stats
    JSON redirected into tmp_path — never at data/."""
    from src.core.config import Config
    from src.core.rawstore import RawStore
    from src.core.runlog import RunContext
    from src.identity.registry import AccountRegistry
    from src.pipeline.runner import CollectorRunner
    from src.signals.store import SignalStore
    from src.signals.taxonomy import Taxonomy
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
        cfg, db, AccountRegistry(db), store, _StubFetch(), SignalStore(db, tax), tax, ctx,
    )
    return runner, db, ctx, stats_path


def test_runner_files_appstore_review_trend_stats(tmp_path, monkeypatch):
    reviews = [
        {
            "review_id": "r1",
            "app_store_id": APP_ID,
            "rating": 4,
            "author": "Jane",
            "title": "Good",
            "body": "Solid app",
            "posted_at": "2026-08-30T08:36:05-07:00",
            "review_url": None,
            "version": "26.08.40",
        }
    ]
    adapter = _AppstoreStub(reviews)
    runner, db, ctx, stats_path = _make_runner(tmp_path, monkeypatch)
    try:
        runner.run([adapter], [Account(domain="acme.com", app_store_id=APP_ID)], force=True)
    finally:
        ctx.__exit__(None, None, None)

    stats = json.loads(stats_path.read_text(encoding="utf-8")) if Path(stats_path).exists() else {}
    key = f"appstore_reviews:{APP_ID}"
    assert key in stats, (
        f"stats file missing {key}; keys={sorted(stats)} — the appstore "
        "review trend can never fire"
    )
    assert stats[key]["count"] == 1
    assert stats[key]["avg_rating"] == 4.0
    # The garbled legacy slice must never appear as a stats key.
    assert f"iews:{APP_ID}" not in stats
    # The review itself persisted through the appstore upsert path.
    row = db.one("SELECT source FROM g2_reviews WHERE review_id=?", (f"{APP_ID}:r1",))
    assert row is not None
    assert row["source"] == "appstore"


# ── (c) im:version capture (dict only — no schema migration) ────────────────


def test_parse_itunes_reviews_captures_version():
    from src.sources.appstores.appstore import parse_itunes_reviews

    reviews = parse_itunes_reviews((FIXTURES / "itunes_reviews_slack.json").read_bytes())
    assert len(reviews) == 50
    assert reviews[0]["version"] == "26.08.40"
    # Every live-fixture entry carries im:version.
    assert all(r["version"] for r in reviews)


def test_parse_version_missing_yields_none_not_error():
    from src.sources.appstores.appstore import parse_itunes_reviews

    body = json.dumps(
        {"feed": {"entry": [{"id": {"label": "1"}, "im:rating": {"label": "5"}}]}}
    ).encode("utf-8")
    revs = parse_itunes_reviews(body)
    assert len(revs) == 1
    assert revs[0]["version"] is None


def test_upsert_tolerates_version_bearing_dicts_without_migration(tmp_path):
    """g2_reviews has no version column and none may be added: the upsert
    must keep accepting the version-bearing dicts unchanged."""
    from src.sources.appstores.appstore import parse_itunes_reviews, upsert_appstore_reviews

    db = Database(tmp_path / "s.db")
    try:
        reviews = parse_itunes_reviews((FIXTURES / "itunes_reviews_slack.json").read_bytes())
        for r in reviews:
            r["app_store_id"] = APP_ID
        new, _updated = upsert_appstore_reviews(
            db, reviews, now="2026-09-04T00:00:00+00:00", raw_ref="doc-9"
        )
        assert new == len(reviews)
        row = db.one("SELECT product_slug FROM g2_reviews WHERE review_id=?", (f"{APP_ID}:14490360492",))
        assert row is not None
        assert row["product_slug"] == f"appstore:{APP_ID}"
        # No migration happened: the table still has no version column.
        assert "version" not in db.table_columns("g2_reviews")
    finally:
        db.close()
