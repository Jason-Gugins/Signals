"""Tests for the pure Capterra review parser (Task 2).

Built against the real rendered Jira reviews fixture captured in the
Task 1 discovery spike (25 review cards in the raw server-rendered HTML).
"""

from pathlib import Path

import pytest

from src.sources.marketplace.capterra import CapterraReview, extract_capterra_reviews

FIXTURE = Path("tests/fixtures/marketplace/capterra_jira_reviews.html")


def test_capterra_review_dataclass_defaults():
    r = CapterraReview(review_id="x", product_slug="jira")
    assert r.rating is None
    assert r.pros == []
    assert r.cons == []
    assert r.nps_score is None
    assert r.helpful_votes is None


@pytest.mark.skipif(not FIXTURE.exists(), reason="fixture not captured yet")
def test_extract_capterra_reviews_count():
    reviews = extract_capterra_reviews(FIXTURE.read_text(encoding="utf-8"), "jira")
    assert len(reviews) >= 1


@pytest.mark.skipif(not FIXTURE.exists(), reason="fixture not captured yet")
def test_extract_capterra_reviews_card_count_matches_container():
    """Every review card in the container yields exactly one CapterraReview."""
    from bs4 import BeautifulSoup

    html = FIXTURE.read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "lxml")
    container = soup.select_one("div[data-test-id='review-cards-container']")
    cards = [
        c
        for c in container.find_all(recursive=False)
        if c.select_one("div[data-testid='Overall Rating-rating']")
    ]
    reviews = extract_capterra_reviews(html, "jira")
    assert len(cards) == 25
    assert len(reviews) == len(cards)


@pytest.mark.skipif(not FIXTURE.exists(), reason="fixture not captured yet")
def test_extract_capterra_reviews_fields():
    r = extract_capterra_reviews(FIXTURE.read_text(encoding="utf-8"), "jira")[0]
    assert r.product_slug == "jira"
    assert r.rating is not None
    assert 0 <= r.rating <= 5
    assert r.posted_at and len(r.posted_at) == 10  # YYYY-MM-DD
    assert r.review_id  # deterministic


@pytest.mark.skipif(not FIXTURE.exists(), reason="fixture not captured yet")
def test_extract_capterra_reviews_all_fields_populated():
    reviews = extract_capterra_reviews(FIXTURE.read_text(encoding="utf-8"), "jira")
    ids = {r.review_id for r in reviews}
    assert len(ids) == len(reviews)  # unique deterministic ids
    assert all(r.posted_at and r.posted_at[4] == "-" and r.posted_at[7] == "-" for r in reviews)
    assert any(r.pros for r in reviews)
    assert any(r.cons for r in reviews)
    assert any(r.reviewer_name for r in reviews)


def test_extract_capterra_reviews_empty_html():
    assert extract_capterra_reviews("", "jira") == []
    assert extract_capterra_reviews("<html><body>no reviews</body></html>", "jira") == []


@pytest.mark.skipif(not FIXTURE.exists(), reason="fixture not captured yet")
def test_month_name_dates_normalized_to_iso():
    """'August 13, 2026' -> '2026-08-13' style month-name parsing."""
    reviews = extract_capterra_reviews(FIXTURE.read_text(encoding="utf-8"), "jira")
    for r in reviews:
        if r.posted_at:
            year, month, day = r.posted_at.split("-")
            assert 1 <= int(month) <= 12
            assert len(year) == 4
