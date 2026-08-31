"""Tests for the pure TrustRadius review parser.

Built against the real rendered Slack reviews fixture captured live in the
Task 2 discovery probe (Aug 2026, curl_cffi chrome impersonation, HTTP 200 —
3 of the page's 10 review cards kept in the fixture).
"""

from pathlib import Path

import pytest

from src.sources.marketplace.trustradius import (
    TrustRadiusReview,
    extract_trustradius_reviews,
)

FIXTURE = Path("tests/fixtures/marketplace/trustradius_slack_reviews.html")


def test_trustradius_review_dataclass_defaults():
    r = TrustRadiusReview(review_id="x", product_slug="slack")
    assert r.rating is None
    assert r.pros == []
    assert r.cons == []
    assert r.nps_score is None
    assert r.helpful_votes is None
    assert r.verified_reviewer is False


@pytest.mark.skipif(not FIXTURE.exists(), reason="fixture not captured yet")
def test_extract_trustradius_reviews_count():
    reviews = extract_trustradius_reviews(FIXTURE.read_text(encoding="utf-8"), "slack")
    assert len(reviews) == 3


@pytest.mark.skipif(not FIXTURE.exists(), reason="fixture not captured yet")
def test_extract_trustradius_reviews_fields():
    r = extract_trustradius_reviews(FIXTURE.read_text(encoding="utf-8"), "slack")[0]
    assert r.product_slug == "slack"
    assert r.rating is not None
    assert 0 <= r.rating <= 5
    assert r.posted_at and len(r.posted_at) == 10  # YYYY-MM-DD
    assert r.review_id  # stable slug id from the review URL
    assert r.review_id == "slack-2026-08-05-00-29-43"


@pytest.mark.skipif(not FIXTURE.exists(), reason="fixture not captured yet")
def test_extract_trustradius_reviews_rating_scale_normalized():
    """TrustRadius data-rating is 0-10; parser normalizes to 0-5 (9 -> 4.5)."""
    reviews = extract_trustradius_reviews(FIXTURE.read_text(encoding="utf-8"), "slack")
    ratings = [r.rating for r in reviews]
    assert ratings[0] == 4.5
    assert all(r is not None and 0 <= r <= 5 for r in ratings)


@pytest.mark.skipif(not FIXTURE.exists(), reason="fixture not captured yet")
def test_extract_trustradius_reviews_all_fields_populated():
    reviews = extract_trustradius_reviews(FIXTURE.read_text(encoding="utf-8"), "slack")
    ids = {r.review_id for r in reviews}
    assert len(ids) == len(reviews)  # unique ids
    assert all(r.review_title for r in reviews)
    assert all(r.reviewer_name for r in reviews)
    assert all(r.review_url and r.review_url.startswith("https://www.trustradius.com/reviews/")
               for r in reviews)
    assert all(r.review_body for r in reviews)
    assert any(r.pros for r in reviews)
    assert any(r.cons for r in reviews)
    assert any(r.reviewer_company_size and "employees" in r.reviewer_company_size
               for r in reviews)
    assert all(r.verified_reviewer for r in reviews)  # all fixture cards are Vetted
    assert all(r.nps_score is None and r.helpful_votes is None for r in reviews)


@pytest.mark.skipif(not FIXTURE.exists(), reason="fixture not captured yet")
def test_extract_trustradius_reviews_dates_iso():
    reviews = extract_trustradius_reviews(FIXTURE.read_text(encoding="utf-8"), "slack")
    for r in reviews:
        year, month, day = r.posted_at.split("-")
        assert len(year) == 4
        assert 1 <= int(month) <= 12
        assert 1 <= int(day) <= 31


def test_extract_trustradius_reviews_empty_html():
    assert extract_trustradius_reviews("", "slack") == []
    assert extract_trustradius_reviews("<html><body>no reviews</body></html>", "slack") == []
