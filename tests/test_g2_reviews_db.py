from src.core.db import Database

def test_g2_reviews_table_exists(tmp_path):
    db = Database(tmp_path / "s.db")
    cols = db.table_columns("g2_reviews")
    expected = {"review_id", "product_slug", "reviewer_name", "reviewer_title",
                "reviewer_company_size", "rating", "review_title", "review_body",
                "pros", "cons", "posted_at", "review_url", "verified_reviewer",
                "review_source", "first_seen_at", "last_seen_at"}
    assert expected.issubset(cols)

def test_g2_reviews_upsert(tmp_path):
    db = Database(tmp_path / "s.db")
    db.upsert("g2_reviews", {
        "review_id": "abc123",
        "product_slug": "slack",
        "reviewer_name": "John D",
        "reviewer_title": "Engineer",
        "reviewer_company_size": "Mid-Market(51-1000 emp.)",
        "rating": 4.5,
        "review_title": "Great tool",
        "review_body": "Slack is great for team communication.",
        "pros": '["Real-time messaging", "Integrations"]',
        "cons": '["Notifications can be overwhelming"]',
        "posted_at": "2026-01-15",
        "review_url": "https://www.g2.com/products/slack/reviews#abc",
        "verified_reviewer": 1,
        "review_source": "Organic",
        "first_seen_at": "2026-08-25T00:00:00+00:00",
        "last_seen_at": "2026-08-25T00:00:00+00:00",
    }, pk=("review_id",))
    row = db.one("SELECT * FROM g2_reviews WHERE review_id=?", ("abc123",))
    assert row["reviewer_name"] == "John D"
    assert row["rating"] == 4.5


def test_g2_reviews_table_has_nps_and_helpful_columns(tmp_path):
    db = Database(tmp_path / "s.db")
    cols = db.table_columns("g2_reviews")
    assert "nps_score" in cols
    assert "helpful_votes" in cols


def test_g2_reviews_table_has_nps_and_helpful_columns_migration(tmp_path):
    # Simulate a pre-existing DB created before the columns were added.
    import sqlite3
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
    cols = db.table_columns("g2_reviews")
    assert "nps_score" in cols
    assert "helpful_votes" in cols


def test_upsert_g2_reviews_persists_nps_and_helpful(tmp_path):
    from src.sources.marketplace.collector import upsert_g2_reviews
    from src.sources.marketplace.g2 import G2Review

    db = Database(tmp_path / "s.db")
    r = G2Review(
        review_id="nps1", product_slug="sierra", rating=4.5,
        nps_score=9, helpful_votes=4,
    )
    new, updated = upsert_g2_reviews(db, [r], now="2026-08-30T00:00:00+00:00")
    assert (new, updated) == (1, 0)
    row = db.one("SELECT * FROM g2_reviews WHERE review_id=?", ("nps1",))
    assert row["nps_score"] == 9
    assert row["helpful_votes"] == 4
