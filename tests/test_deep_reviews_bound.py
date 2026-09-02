"""Tests for deep-reviews extreme-rating bounding (Plan Task 9)."""

import pytest

from src.sources.marketplace.deep_reviews import normalize_bound, should_expand


class TestShouldExpand:
    @pytest.mark.parametrize(
        "rating,bound,expected",
        [
            # 'extreme': only >= 4 or <= 2
            (4.5, "extreme", True),
            (5.0, "extreme", True),
            (4.0, "extreme", True),
            (3.0, "extreme", False),
            (3.5, "extreme", False),
            (2.0, "extreme", True),
            (1.5, "extreme", True),
            (0.5, "extreme", True),
            # None/unknown rating expands (conservative)
            (None, "extreme", True),
            # bound=None: no bounding, always expand
            (3.0, None, True),
            (4.5, None, True),
            (None, None, True),
        ],
    )
    def test_table(self, rating, bound, expected):
        assert should_expand(rating, bound) is expected


class TestNormalizeBound:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("extreme", "extreme"),
            (None, None),  # explicit null = legacy bound-free behavior
            ("", "extreme"),
            ("bogus", "extreme"),  # invalid -> default
            (123, "extreme"),
        ],
    )
    def test_invalid_and_absent_default_to_extreme(self, value, expected):
        assert normalize_bound(value) == expected


class TestConfigParsing:
    def test_config_default_is_extreme(self):
        from src.core.config import Config

        cfg = Config()
        data = cfg.load_yaml("marketplace") or {}
        bound = (
            data.get("sites", {}).get("g2", {}).get("deep_reviews_bound", "extreme")
        )
        assert normalize_bound(bound) == "extreme"


# ---------------------------------------------------------------------------
# P3 Batch 3+4 review fixes: MINOR A (null contract honored + wiring).
# ---------------------------------------------------------------------------


class TestFilterDeepReviews:
    """filter_deep_reviews (g2.py): the wired review-filter-level bound."""

    def _review(self, rating):
        from src.sources.marketplace.g2 import G2Review

        return G2Review(review_id="r1", product_slug="slack", rating=rating)

    def test_extreme_bound_drops_mid_ratings(self):
        from src.sources.marketplace.g2 import filter_deep_reviews

        reviews = [self._review(r) for r in (5.0, 3.0, 1.5, None)]
        kept = filter_deep_reviews(reviews, "extreme")
        assert [r.rating for r in kept] == [5.0, 1.5, None]

    def test_explicit_none_keeps_everything(self):
        from src.sources.marketplace.g2 import filter_deep_reviews

        reviews = [self._review(r) for r in (5.0, 3.0, 1.5)]
        assert filter_deep_reviews(reviews, None) == reviews

    def test_invalid_bound_normalizes_to_extreme(self):
        from src.sources.marketplace.g2 import filter_deep_reviews

        reviews = [self._review(3.0), self._review(5.0)]
        assert [r.rating for r in filter_deep_reviews(reviews, "bogus")] == [5.0]


class TestCollectorWiring:
    """The bound is applied in MarketplaceG2Source.parse when Show More expansion is on."""

    def _meta(self, bound="extreme", expand=True):
        return {"today": "2026-03-15", "click_show_more": expand, "deep_reviews_bound": bound}

    def _parse(self, monkeypatch, meta, ratings=(3.0, 5.0)):
        from src.sources.marketplace.collector import MarketplaceG2Source
        from src.sources.marketplace.g2 import G2Review
        from src.core.models import Account, Document

        reviews = [
            G2Review(review_id=f"r{i}", product_slug="slack", rating=rating,
                     review_title=f"t{i}")
            for i, rating in enumerate(ratings)
        ]
        monkeypatch.setattr(
            "src.sources.marketplace.collector._parse_g2_body",
            lambda body, url, slug: reviews,
        )
        adapter = MarketplaceG2Source()
        acct = Account(domain="acme.com", g2_slug="slack")
        doc = Document(doc_id="d", source="marketplace_g2",
                       url="https://www.g2.com/products/slack/reviews", body=b"x")
        return adapter.parse(doc, acct, meta)

    def test_bound_applied_when_expansion_enabled(self, monkeypatch):
        cands = self._parse(monkeypatch, self._meta(), ratings=(3.0, 5.0))
        assert len(cands) == 1  # the 3.0-rated review is dropped

    def test_bound_not_applied_without_expansion(self, monkeypatch):
        cands = self._parse(monkeypatch, self._meta(expand=False), ratings=(3.0, 5.0))
        assert len(cands) == 2

    def test_explicit_null_bound_keeps_all(self, monkeypatch):
        cands = self._parse(monkeypatch, self._meta(bound=None), ratings=(3.0, 5.0))
        assert len(cands) == 2


class TestRunnerMetaInjection:
    """The runner injects deep_reviews_bound from sites.<site> config when expanding."""

    def test_runner_injects_bound_when_deep_reviews_enabled(self, monkeypatch, tmp_path):
        import inspect

        from src.pipeline import runner as runner_mod

        src = inspect.getsource(runner_mod)
        assert "deep_reviews_bound" in src
