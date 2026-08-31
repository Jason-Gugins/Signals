"""DB persistence for TrustRadius reviews in the shared g2_reviews table.

Mirrors tests/test_capterra_db.py: upsert_trustradius_reviews writes
source='trustradius' rows and is idempotent (no duplicates on re-upsert).
"""
from src.core.db import Database
from src.sources.marketplace.trustradius import TrustRadiusReview
from src.sources.marketplace.collector import upsert_trustradius_reviews


def _tr_review(**overrides):
    fields = dict(
        review_id="slack-2026-08-05-00-29-43",
        product_slug="slack",
        reviewer_name="Rushikesh Vasande",
        reviewer_title="Full-stack developer in Information Technology at SDLC Corp",
        reviewer_company_size="501-1000 employees",
        rating=4.5,
        review_title="Well Organized and Easy to Manage",
        review_body="My team uses Slack every day for communication.",
        pros=["Keep project discussions organized with separate channels."],
        cons=["Too many notification can be distracting"],
        posted_at="2026-08-10",
        review_url="https://www.trustradius.com/reviews/slack-2026-08-05-00-29-43",
        verified_reviewer=True,
        review_source=None,
    )
    fields.update(overrides)
    return TrustRadiusReview(**fields)


def test_upsert_trustradius_reviews_sets_source_and_round_trips(tmp_path):
    db = Database(tmp_path / "s.db")
    new, updated = upsert_trustradius_reviews(
        db, [_tr_review()], now="2026-08-30T00:00:00+00:00", raw_ref="doc-1"
    )
    assert (new, updated) == (1, 0)
    row = db.one("SELECT * FROM g2_reviews WHERE review_id=?",
                 ("slack-2026-08-05-00-29-43",))
    assert row is not None
    assert row["source"] == "trustradius"
    assert row["product_slug"] == "slack"
    assert row["reviewer_name"] == "Rushikesh Vasande"
    assert row["rating"] == 4.5
    assert row["review_title"] == "Well Organized and Easy to Manage"
    assert row["posted_at"] == "2026-08-10"
    # nps/helpful are None for TrustRadius (no markup maps them).
    assert row["nps_score"] is None and row["helpful_votes"] is None


def test_upsert_trustradius_reviews_idempotent(tmp_path):
    db = Database(tmp_path / "s.db")
    upsert_trustradius_reviews(db, [_tr_review()], now="2026-08-30T00:00:00+00:00")
    new, updated = upsert_trustradius_reviews(
        db, [_tr_review()], now="2026-08-30T01:00:00+00:00"
    )
    assert (new, updated) == (0, 1)
    count = db.one("SELECT COUNT(*) AS n FROM g2_reviews WHERE review_id=?",
                   ("slack-2026-08-05-00-29-43",))
    assert count["n"] == 1


def test_upsert_trustradius_does_not_clobber_other_sources(tmp_path):
    """TrustRadius ids are URL slugs — they can never collide with G2 numeric
    ids or Capterra sha256 hashes in the shared table."""
    db = Database(tmp_path / "s.db")
    upsert_trustradius_reviews(db, [_tr_review()], now="2026-08-30T00:00:00+00:00")
    rows = [db.one("SELECT source FROM g2_reviews WHERE review_id=?",
                   ("slack-2026-08-05-00-29-43",))]
    assert [r["source"] for r in rows] == ["trustradius"]
