"""Confidence calibration: blend from observed per-source hit rates (P1 Task 6)."""

from datetime import date
from pathlib import Path

import pytest

from src.core.db import Database
from src.core.models import Account, Signal
from src.signals.calibration import blend_confidence, load_stats
from src.signals.score import score_account
from src.signals.taxonomy import Taxonomy


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "cal.db")


class TestBlendConfidence:
    def test_stats_none_returns_base(self):
        assert blend_confidence(0.8, "g2", "review_velocity", None) == 0.8

    def test_unknown_key_returns_base(self):
        stats = {}
        assert blend_confidence(0.8, "g2", "review_velocity", stats) == 0.8

    def test_below_min_samples_returns_base(self):
        stats = {("g2", "review_velocity"): {"samples": 29, "hits": 29}}
        assert blend_confidence(0.8, "g2", "review_velocity", stats) == 0.8

    def test_blend_at_exactly_min_samples(self):
        # 30 samples, 15 hits -> rate 0.5 -> factor 0.5 + 0.5*0.5 = 0.75
        stats = {("g2", "review_velocity"): {"samples": 30, "hits": 15}}
        assert blend_confidence(0.8, "g2", "review_velocity", stats) == pytest.approx(0.6)

    def test_perfect_rate_clamps_to_max(self):
        # rate 1.0 -> factor 1.0 -> base*1.0, but clamp upper bound 0.95
        stats = {("g2", "review_velocity"): {"samples": 100, "hits": 100}}
        assert blend_confidence(0.99, "g2", "review_velocity", stats) == pytest.approx(0.95)

    def test_zero_rate_clamps_to_min(self):
        stats = {("g2", "review_velocity"): {"samples": 50, "hits": 0}}
        assert blend_confidence(0.1, "g2", "review_velocity", stats) == pytest.approx(0.05)

    def test_min_samples_override(self):
        stats = {("g2", "review_velocity"): {"samples": 4, "hits": 2}}
        assert blend_confidence(0.8, "g2", "review_velocity", stats, min_samples=4) == pytest.approx(0.6)


class TestLoadStats:
    def test_seeded_table(self, db):
        db.execute(
            "INSERT INTO calibration (source, signal_type, samples, hits) VALUES (?, ?, ?, ?)",
            ("g2", "review_velocity", 40, 30),
        )
        db.execute(
            "INSERT INTO calibration (source, signal_type, samples, hits) VALUES (?, ?, ?, ?)",
            ("capterra", "review_velocity", 10, 5),
        )
        db.conn.commit()
        stats = load_stats(db)
        assert stats[("g2", "review_velocity")] == {"samples": 40, "hits": 30}
        assert stats[("capterra", "review_velocity")] == {"samples": 10, "hits": 5}

    def test_empty_table(self, db):
        assert load_stats(db) == {}


class TestMigrationV2:
    def test_calibration_table_exists(self, db):
        cols = db.table_columns("calibration")
        assert {"source", "signal_type", "samples", "hits"} <= cols

    def test_primary_key(self, db):
        pk = [
            r["name"]
            for r in db.query("PRAGMA table_info(calibration)")
            if r["pk"]
        ]
        assert pk == ["source", "signal_type"]

    def test_user_version_at_latest(self, db):
        from src.core.db import LATEST_VERSION

        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_VERSION


class TestScoreIntegration:
    """score_account with no calibration stats is a no-op; with stats it blends."""

    TODAY = date(2026, 8, 31)
    TAX = Taxonomy.load(str(Path("config/signals.yaml")))

    def _sig(self, conf=0.8, source="g2", typ="funding_round"):
        spec = self.TAX.get(typ)
        return Signal(
            signal_id="s1",
            domain="acme.com",
            signal_type=typ,
            category=spec.category,
            origin=spec.origin,
            catalyst=spec.catalyst,
            polarity=spec.polarity,
            observed_at="2026-08-01",
            source=source,
            confidence=conf,
        )

    def _score(self, sig, stats=None):
        return score_account(
            Account(domain="acme.com", icp_fit=1.0),
            [sig],
            taxonomy=self.TAX,
            cfg={},
            today=self.TODAY,
            combos=[],
            calibration_stats=stats,
        )

    def test_no_stats_unchanged(self):
        result = self._score(self._sig())
        assert result.contributions[0].confidence == 0.8

    def test_with_stats_blended(self):
        stats = {("g2", "funding_round"): {"samples": 30, "hits": 15}}
        result = self._score(self._sig(), stats=stats)
        assert result.contributions[0].confidence == pytest.approx(0.6)

    def test_below_min_samples_unchanged(self):
        stats = {("g2", "funding_round"): {"samples": 10, "hits": 10}}
        result = self._score(self._sig(), stats=stats)
        assert result.contributions[0].confidence == 0.8
