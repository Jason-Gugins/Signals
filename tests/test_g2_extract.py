"""Tests for extract_g2_reviews — parses G2's current elv-* review DOM.

TDD: these tests are written against the REAL live DOM fixtures
(tests/fixtures/marketplace/g2_reviews_live_*.html) so the selector set is
locked to what G2 actually renders today, not a frozen itempropage.
"""

from __future__ import annotations

import re
from pathlib import Path

from src.sources.marketplace.g2 import G2Review, extract_g2_reviews

FIXTURES = Path("tests/fixtures/marketplace")
SIERRA_HTML = (FIXTURES / "g2_reviews_live_sierra.html").read_text(encoding="utf-8")
HELCIM_HTML = (FIXTURES / "g2_reviews_live_helcim.html").read_text(encoding="utf-8")


def _extract(html: str, slug: str) -> list[G2Review]:
    return extract_g2_reviews(html, slug)


def test_extract_g2_reviews_sierra_counts():
    reviews = _extract(SIERRA_HTML, "sierra")
    assert len(reviews) >= 1
    assert len(reviews) == 10


def test_extract_g2_reviews_sierra_fields():
    reviews = _extract(SIERRA_HTML, "sierra")
    for r in reviews:
        assert r.product_slug == "sierra"
        assert isinstance(r.rating, float)
        assert r.review_title
        assert r.pros
        assert r.cons
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", r.posted_at or "")
        assert r.review_url
        assert r.review_id
    assert all(isinstance(r, G2Review) for r in reviews)


def test_extract_g2_reviews_sierra_exact_rating():
    """Lock the 0-5 scale: first Sierra review is 4.5 (elv-stars-9)."""
    reviews = _extract(SIERRA_HTML, "sierra")
    assert reviews[0].rating == 4.5


def test_extract_g2_reviews_review_id_deterministic():
    a = _extract(SIERRA_HTML, "sierra")
    b = _extract(SIERRA_HTML, "sierra")
    assert [r.review_id for r in a] == [r.review_id for r in b]
    # ids are distinct across reviews, so dedupe works
    assert len({r.review_id for r in a}) == len(a)


def test_extract_g2_reviews_helcim_works():
    reviews = _extract(HELCIM_HTML, "helcim")
    assert len(reviews) >= 1
    assert all(r.product_slug == "helcim" for r in reviews)
    for r in reviews:
        assert isinstance(r.rating, float)
        assert r.review_title
        assert r.pros
        assert r.cons
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", r.posted_at or "")


def test_extract_g2_reviews_helcim_exact_rating():
    """Second fixture sanity: first Helcim review is 1.0 (ratings vary)."""
    reviews = _extract(HELCIM_HTML, "helcim")
    assert reviews[0].rating == 1.0
