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
