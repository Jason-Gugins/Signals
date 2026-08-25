from datetime import datetime, timezone
from pathlib import Path
from src.core.db import Database
from src.sources.marketplace.g2 import parse_g2_reviews

FIXTURE = (Path("tests/fixtures/marketplace/g2_reviews.html")).read_text(encoding="utf-8")

def test_upsert_g2_reviews(tmp_path):
    from src.sources.marketplace.collector import upsert_g2_reviews

    db = Database(tmp_path / "s.db")
    reviews = parse_g2_reviews(FIXTURE, "https://www.g2.com/products/slack/reviews")
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    upsert_g2_reviews(db, reviews, now=now, raw_ref="doc123")
    rows = db.query("SELECT * FROM g2_reviews WHERE product_slug=?", ("slack",))
    assert len(rows) == 3
    assert rows[0]["rating"] == 4.5
    # Upsert is idempotent — same reviews don't create duplicates
    upsert_g2_reviews(db, reviews, now=now, raw_ref="doc123")
    rows2 = db.query("SELECT * FROM g2_reviews WHERE product_slug=?", ("slack",))
    assert len(rows2) == 3
