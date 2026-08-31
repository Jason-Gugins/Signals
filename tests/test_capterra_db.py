"""Task 5: DB persistence for Capterra reviews in the shared g2_reviews table.

The table gains a `source TEXT DEFAULT 'g2'` column via the NEW_COLUMNS
migration; upsert_capterra_reviews writes source='capterra' rows and is
idempotent (no duplicates on re-upsert).
"""
import sqlite3

from src.core.db import Database
from src.sources.marketplace.capterra import CapterraReview
from src.sources.marketplace.collector import (
    upsert_capterra_reviews,
    upsert_g2_reviews,
)


def test_g2_reviews_table_has_source_column(tmp_path):
    db = Database(tmp_path / "s.db")
    assert "source" in db.table_columns("g2_reviews")


def test_g2_reviews_source_column_migration_on_old_db(tmp_path):
    """Pre-existing DB without the column gets it via NEW_COLUMNS migration."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE g2_reviews (
            review_id TEXT PRIMARY KEY, product_slug TEXT NOT NULL,
            reviewer_name TEXT, reviewer_title TEXT, reviewer_company_size TEXT,
            rating REAL, review_title TEXT, review_body TEXT, pros TEXT, cons TEXT,
            posted_at TEXT, review_url TEXT, verified_reviewer INTEGER DEFAULT 0,
            review_source TEXT, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
            raw_ref TEXT);
    """)
    conn.commit()
    conn.close()
    db = Database(path)
    assert "source" in db.table_columns("g2_reviews")


def _capterra_review(**overrides):
    fields = dict(
        review_id="cap-hash-0001",
        product_slug="19319/JIRA",
        reviewer_name="Kim F.",
        reviewer_title="Manager Appeals",
        reviewer_company_size=None,
        rating=5.0,
        review_title="Task Management Like A PRO!",
        review_body="An incredible tool to create synergies among the team.",
        pros=["Seamless project view"],
        cons=["Minimal issues"],
        posted_at="2026-06-29",
        review_url="https://www.capterra.com/p/19319/JIRA/reviews/",
        verified_reviewer=False,
        review_source="Incentivized",
    )
    fields.update(overrides)
    return CapterraReview(**fields)


def test_upsert_capterra_reviews_sets_source_and_round_trips(tmp_path):
    db = Database(tmp_path / "s.db")
    new, updated = upsert_capterra_reviews(
        db, [_capterra_review()], now="2026-08-30T00:00:00+00:00", raw_ref="doc-1"
    )
    assert (new, updated) == (1, 0)
    row = db.one("SELECT * FROM g2_reviews WHERE review_id=?", ("cap-hash-0001",))
    assert row is not None
    assert row["source"] == "capterra"
    assert row["product_slug"] == "19319/JIRA"
    assert row["reviewer_name"] == "Kim F."
    assert row["rating"] == 5.0
    assert row["review_title"] == "Task Management Like A PRO!"
    assert row["posted_at"] == "2026-06-29"
    # nps/helpful are None for Capterra (no markup maps them).
    assert row["nps_score"] is None and row["helpful_votes"] is None


def test_upsert_capterra_reviews_idempotent(tmp_path):
    db = Database(tmp_path / "s.db")
    upsert_capterra_reviews(db, [_capterra_review()], now="2026-08-30T00:00:00+00:00")
    new, updated = upsert_capterra_reviews(
        db, [_capterra_review()], now="2026-08-30T01:00:00+00:00"
    )
    assert (new, updated) == (0, 1)
    count = db.one("SELECT COUNT(*) AS n FROM g2_reviews WHERE review_id=?", ("cap-hash-0001",))
    assert count["n"] == 1


def test_upsert_g2_reviews_defaults_source_to_g2(tmp_path):
    db = Database(tmp_path / "s.db")

    class G2Rev:
        review_id = "12345"
        product_slug = "sierra"
        reviewer_name = "A B"
        reviewer_title = None
        reviewer_company_size = None
        rating = 4.0
        review_title = "Solid"
        review_body = "body"
        pros = ["x"]
        cons = ["y"]
        posted_at = "2026-07-01"
        review_url = "https://www.g2.com/products/sierra/reviews#12345"
        verified_reviewer = True
        review_source = "Organic"
        nps_score = None
        helpful_votes = None

    upsert_g2_reviews(db, [G2Rev()], now="2026-08-30T00:00:00+00:00")
    row = db.one("SELECT source FROM g2_reviews WHERE review_id=?", ("12345",))
    assert row["source"] == "g2"
