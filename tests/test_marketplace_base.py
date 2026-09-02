"""Tests for the MarketplaceReview normalizing base (Plan Task 8)."""

import pytest

from src.sources.marketplace.base import MarketplaceReview


def _raw(**overrides):
    base = {
        "review_id": "r1",
        "product_slug": "acme",
        "reviewer": "Jane",
        "rating": "4.5",
        "posted": "2026-01-01",
        "body": "Great product",
        "source": "g2",
        "url": "https://g2.com/reviews/r1",
    }
    base.update(overrides)
    return base


class TestRatingCoercion:
    """Coercion table: rating str/int/None/float/junk -> float or 0.0, never raises."""

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("4.5", 4.5),
            ("4", 4.0),
            (4, 4.0),
            (5, 5.0),
            (4.5, 4.5),
            (None, 0.0),
            ("", 0.0),
            ("junk", 0.0),
            ("  3.2  ", 3.2),
            (object(), 0.0),
            ("-1.5", -1.5),
        ],
    )
    def test_coercion(self, value, expected):
        raw = _raw(rating=value)
        mr = MarketplaceReview.from_raw(raw)
        assert mr.rating == expected
        assert isinstance(mr.rating, float)

    def test_never_raises_on_weird_payload(self):
        raw = _raw()
        raw["rating"] = {"nested": True}
        assert MarketplaceReview.from_raw(raw).rating == 0.0

    def test_missing_rating_key(self):
        raw = _raw()
        del raw["rating"]
        assert MarketplaceReview.from_raw(raw).rating == 0.0


class TestFields:
    def test_field_mapping(self):
        mr = MarketplaceReview.from_raw(_raw(helpful_votes=7, nps=9))
        assert mr.review_id == "r1"
        assert mr.product_slug == "acme"
        assert mr.reviewer == "Jane"
        assert mr.posted == "2026-01-01"
        assert mr.body == "Great product"
        assert mr.source == "g2"
        assert mr.url == "https://g2.com/reviews/r1"
        assert mr.helpful_votes == 7
        assert mr.nps == 9

    def test_optional_default_none(self):
        mr = MarketplaceReview.from_raw(_raw())
        assert mr.helpful_votes is None
        assert mr.nps is None
