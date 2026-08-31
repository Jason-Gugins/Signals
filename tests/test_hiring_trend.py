"""Tests for the pure hiring-velocity trend signal (jobsignals)."""

from __future__ import annotations

from datetime import date

from src.sources.jobsignals.trend import (
    hiring_trend_signal,
    load_stats,
    save_stats,
    stats_key,
)

TODAY = date(2026, 8, 31)


def test_crossing_threshold_emits_hiring_surge():
    prev = {"count": 10}
    curr = {"count": 14}  # +40%
    cand = hiring_trend_signal("acme.com", prev, curr, today=TODAY)
    assert cand is not None
    assert cand.signal_type == "hiring_surge"
    assert cand.evidence_data["delta_pct"] == 40.0
    assert cand.evidence_data["count_delta"] == 4
    assert cand.evidence_data["prev"] == {"count": 10}
    assert cand.evidence_data["current"] == {"count": 14}
    assert cand.evidence_data["thresholds"]["min_delta_pct"] == 25.0


def test_below_threshold_returns_none():
    prev = {"count": 10}
    curr = {"count": 12}  # +20% < 25%
    assert hiring_trend_signal("acme.com", prev, curr, today=TODAY) is None


def test_decrease_returns_none():
    prev = {"count": 10}
    curr = {"count": 5}
    assert hiring_trend_signal("acme.com", prev, curr, today=TODAY) is None


def test_first_run_prev_none_returns_none():
    curr = {"count": 40}
    assert hiring_trend_signal("acme.com", None, curr, today=TODAY) is None


def test_zero_baseline_returns_none():
    # 0 -> 3 is an infinite ratio; without a baseline the percentage is
    # meaningless, so no signal.
    assert hiring_trend_signal("acme.com", {"count": 0}, {"count": 3}, today=TODAY) is None


def test_natural_key_stable():
    prev = {"count": 10}
    curr = {"count": 20}
    c1 = hiring_trend_signal("acme.com", prev, curr, today=TODAY)
    c2 = hiring_trend_signal("acme.com", prev, curr, today="2026-08-31")
    assert c1 is not None and c2 is not None
    assert c1.natural_key == c2.natural_key == "hrtrend:acme.com:2026-08-31"
    # idempotent: same inputs -> same natural key (upsert dedupes)
    assert c1.natural_key == hiring_trend_signal(
        "acme.com", prev, curr, today=date(2026, 8, 31)
    ).natural_key


def test_custom_threshold():
    prev = {"count": 10}
    curr = {"count": 12}
    cand = hiring_trend_signal("acme.com", prev, curr, today=TODAY, min_delta_pct=15.0)
    assert cand is not None
    assert cand.evidence_data["delta_pct"] == 20.0
    # and the default threshold rejects the same delta
    assert hiring_trend_signal("acme.com", prev, curr, today=TODAY) is None


def test_stats_key_is_domain():
    assert stats_key("acme.com") == "acme.com"


def test_stats_roundtrip(tmp_path):
    path = tmp_path / "stats.json"
    save_stats({"acme.com": {"count": 10}}, path)
    assert load_stats(path) == {"acme.com": {"count": 10}}


def test_load_stats_missing_or_corrupt(tmp_path):
    assert load_stats(tmp_path / "nope.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert load_stats(bad) == {}
