"""Task 9: App Store review source (iTunes RSS JSON).

Covers: pure parsing of the live Slack fixture (50 entries, field
extraction), the dead-app-id empty shell ([] sentinel, not an error),
adapter plan() gating on the app_store_id account field, upsert into the
shared g2_reviews table with source='appstore', and the NEW_COLUMNS
migration that adds the 3 new account columns to legacy DBs.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.db import Database, NEW_COLUMNS  # noqa: E402
from core.models import Account  # noqa: E402
from sources.appstores.appstore import (  # noqa: E402
    AppStoreReviewSource,
    itunes_reviews_url,
    parse_itunes_reviews,
    upsert_appstore_reviews,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "appstores"


def _live_body() -> bytes:
    return (FIXTURES / "itunes_reviews_slack.json").read_bytes()


def test_itunes_reviews_url_shape():
    url = itunes_reviews_url("618783545")
    assert url == (
        "https://itunes.apple.com/us/rss/customerreviews/"
        "id=618783545/sortby=mostrecent/json"
    )
    assert itunes_reviews_url("618783545", country="gb").startswith(
        "https://itunes.apple.com/gb/rss/customerreviews/"
    )


def test_parse_live_fixture_yields_50_entries():
    reviews = parse_itunes_reviews(_live_body())
    assert len(reviews) == 50


def test_parse_extracts_rating_author_id_fields():
    reviews = parse_itunes_reviews(_live_body())
    first = reviews[0]
    # Shape matches the spike: title.label / im:rating.label /
    # author.name.label / content.label / id.label (stable review id).
    assert first["review_id"] == "14490360492"
    assert first["rating"] == 4
    assert first["author"] == "vshultz"
    assert first["title"] == "Loved the old slack, like the new"
    assert "old Slack" in first["body"]
    assert first["posted_at"] == "2026-08-30T08:36:05-07:00"
    # Ratings parse as ints across the feed; ids are unique (dedupe key).
    assert all(isinstance(r["rating"], int) for r in reviews)
    ids = [r["review_id"] for r in reviews]
    assert len(set(ids)) == len(ids)


def test_parse_empty_shell_returns_list_not_error():
    """Dead app id -> 200 shell feed with NO entry key. [] is the sentinel."""
    body = (FIXTURES / "itunes_reviews_empty.json").read_bytes()
    feed = json.loads(body)["feed"]
    assert "entry" not in feed
    assert parse_itunes_reviews(body) == []
    # Robustness: junk/empty bodies also yield [] rather than raising.
    assert parse_itunes_reviews(b"") == []
    assert parse_itunes_reviews(b"not json at all") == []


def test_adapter_plan_requires_app_store_id():
    adapter = AppStoreReviewSource()
    assert adapter.key == "appstore_reviews"
    assert adapter.tier == "http"
    assert adapter.cadence_hours == 168
    assert adapter.requires == ("app_store_id",)

    # No app_store_id field -> empty plan (account gating).
    bare = Account(domain="acme.com")
    assert adapter.plan(bare, cursor=None) == []

    acct = Account(domain="acme.com", app_store_id="618783545")
    tasks = adapter.plan(acct, cursor=None)
    assert len(tasks) == 1
    task = tasks[0]
    assert task.source == "appstore_reviews"
    assert "id=618783545" in task.url
    assert "sortby=mostrecent" in task.url
    assert task.meta["app_store_id"] == "618783545"

    # parse() stays silent: reviews flow through harvest_reviews instead.
    assert adapter.parse(None, acct, {}) == []


def test_harvest_reviews_stamps_app_store_id():
    adapter = AppStoreReviewSource()
    acct = Account(domain="slack.com", app_store_id="618783545")

    class Doc:  # minimal stand-in for Document
        body = _live_body()
        url = "https://itunes.apple.com/us/rss/customerreviews/id=618783545/json"

    revs = adapter.harvest_reviews(Doc(), acct, {"app_store_id": "618783545"})
    assert len(revs) == 50
    assert all(r["app_store_id"] == "618783545" for r in revs)
    # Empty shell still harvests cleanly.
    class EmptyDoc:
        body = (FIXTURES / "itunes_reviews_empty.json").read_bytes()
        url = ""

    assert adapter.harvest_reviews(EmptyDoc(), acct, {"app_store_id": "618783545"}) == []


def test_upsert_appstore_reviews_writes_source_appstore(tmp_path):
    db = Database(tmp_path / "s.db")
    try:
        reviews = parse_itunes_reviews(_live_body())
        for r in reviews:
            r["app_store_id"] = "618783545"
        new, updated = upsert_appstore_reviews(
            db, reviews, now="2026-08-31T00:00:00+00:00", raw_ref="doc-9"
        )
        assert (new, updated) == (50, 0)
        row = db.one(
            "SELECT * FROM g2_reviews WHERE review_id=?", ("618783545:14490360492",)
        )
        assert row is not None
        assert row["source"] == "appstore"
        assert row["product_slug"] == "appstore:618783545"
        assert row["reviewer_name"] == "vshultz"
        assert row["rating"] == 4
        assert row["review_title"] == "Loved the old slack, like the new"
        assert row["raw_ref"] == "doc-9"
        # Idempotent re-upsert: same ids update, no duplicates.
        new2, updated2 = upsert_appstore_reviews(
            db, reviews, now="2026-08-31T01:00:00+00:00", raw_ref="doc-9"
        )
        assert (new2, updated2) == (0, 50)
        count = db.one(
            "SELECT COUNT(*) AS n FROM g2_reviews WHERE source='appstore'"
        )
        assert count["n"] == 50
    finally:
        db.close()


def test_new_columns_migration_adds_account_columns(tmp_path):
    """Legacy DB (pre-Task-9 accounts table) gains the 3 new columns on
    reopen via the additive NEW_COLUMNS pass (pattern from
    tests/test_db_migrations.py)."""
    assert set(NEW_COLUMNS["accounts"]) >= {
        "app_store_id", "play_id", "subreddit"
    }
    path = tmp_path / "legacy.db"
    # Build a current DB, then regress the accounts table to its pre-Task-9
    # shape (no app_store_id/play_id/subreddit) — the rename pattern from
    # tests/test_db_migrations.py.
    db = Database(path)
    db.execute("ALTER TABLE accounts RENAME TO accounts_new")
    db.execute(
        """
        CREATE TABLE accounts (
            domain              TEXT PRIMARY KEY,
            name                TEXT,
            legal_name          TEXT,
            linkedin_slug       TEXT,
            linkedin_company_id TEXT,
            repvue_slug         TEXT,
            g2_slug             TEXT,
            cik                 TEXT,
            ticker              TEXT,
            ats_vendor          TEXT,
            ats_token           TEXT,
            careers_url         TEXT,
            blog_feed_url       TEXT,
            industry            TEXT,
            sic_code            TEXT,
            employee_count      INTEGER,
            employee_count_at   TEXT,
            hq_country          TEXT,
            hq_region           TEXT,
            hq_city             TEXT,
            founded             TEXT,
            company_type        TEXT,
            cohort              TEXT,
            seed_source         TEXT,
            icp_fit             REAL,
            icp_reasons         TEXT,
            disqualified        INTEGER DEFAULT 0,
            disqualify_reason   TEXT,
            score               REAL,
            tier                INTEGER,
            buying_window       TEXT,
            scored_at           TEXT,
            extra_data          TEXT,
            created_at          TEXT DEFAULT (datetime('now')),
            updated_at          TEXT DEFAULT (datetime('now'))
        )
        """
    )
    db.execute(
        "INSERT INTO accounts (domain, name) VALUES ('acme.com', 'Acme')"
    )
    db.execute("DROP TABLE accounts_new")
    db.execute("PRAGMA user_version = 0")
    for col in ("app_store_id", "play_id", "subreddit"):
        assert col not in db.table_columns("accounts")
    db.close()

    db = Database(path)
    try:
        cols = db.table_columns("accounts")
        for col in ("app_store_id", "play_id", "subreddit"):
            assert col in cols
        # The columns must be usable: stamp values and read them back.
        db.execute(
            "UPDATE accounts SET app_store_id=?, play_id=?, subreddit=? "
            "WHERE domain='acme.com'",
            ("618783545", "com.acme.app", "acme"),
        )
        row = db.one(
            "SELECT app_store_id, play_id, subreddit FROM accounts "
            "WHERE domain='acme.com'"
        )
        assert row == {
            "app_store_id": "618783545",
            "play_id": "com.acme.app",
            "subreddit": "acme",
        }
        # Round-trip through the dataclass too: from_db_row accepts the new
        # columns and to_db_row emits them.
        from core.models import Account as A

        acct = A.from_db_row(db.one("SELECT * FROM accounts WHERE domain='acme.com'"))
        assert acct.app_store_id == "618783545"
        assert acct.play_id == "com.acme.app"
        assert acct.subreddit == "acme"
        assert acct.to_db_row()["app_store_id"] == "618783545"
    finally:
        db.close()
