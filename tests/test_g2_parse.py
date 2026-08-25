"""Tests for the pure G2 review parser (src.sources.marketplace.g2)."""

from __future__ import annotations

from pathlib import Path

from src.sources.marketplace.g2 import G2Review, parse_g2_reviews

HTML = (Path("tests/fixtures/marketplace/g2_reviews.html")).read_text(encoding="utf-8")
URL = "https://www.g2.com/products/slack/reviews"


def test_parse_g2_reviews_returns_list():
    reviews = parse_g2_reviews(HTML, URL)
    assert isinstance(reviews, list)
    assert len(reviews) == 3
    assert all(isinstance(r, G2Review) for r in reviews)


def test_parse_g2_reviews_extracts_fields():
    reviews = parse_g2_reviews(HTML, URL)
    r = reviews[0]
    assert r.reviewer_name == "John D"
    assert r.reviewer_title == "Software Engineer"
    assert r.reviewer_company_size == "Mid-Market(51-1000 emp.)"
    assert r.review_title == "Great collaboration tool"
    assert r.rating == 4.5
    assert r.posted_at == "2026-01-15"
    assert "transformed" in (r.review_body or "")
    assert r.pros == ["Real-time messaging", "Integrations with other tools"]
    assert r.cons == ["Notifications can be overwhelming"]
    assert r.verified_reviewer is True
    assert r.review_source == "Organic"
    assert r.product_slug == "slack"
    assert r.review_id  # non-empty
    assert r.review_url == URL


def test_parse_g2_reviews_anonymous_reviewer():
    reviews = parse_g2_reviews(HTML, URL)
    r = reviews[2]
    assert r.reviewer_name == "Anonymous User"
    assert r.rating == 3.5
    assert r.reviewer_title == "IT Department"
    assert r.reviewer_company_size == "Small-Business(50 or fewer emp.)"


def test_parse_g2_reviews_invitation_source():
    reviews = parse_g2_reviews(HTML, URL)
    r = reviews[1]
    assert r.review_source == "Invitation from G2 (Original )"
    assert r.verified_reviewer is True
    assert r.reviewer_name == "Jane S"
    assert r.rating == 5.0


def test_parse_g2_reviews_empty_html():
    reviews = parse_g2_reviews("<html><body></body></html>", URL)
    assert reviews == []
