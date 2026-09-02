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
            (None, "extreme"),  # absent -> new default
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
