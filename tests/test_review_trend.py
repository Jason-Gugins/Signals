"""Tests for the pure marketplace review-velocity trend signal."""

from __future__ import annotations

from datetime import date

from src.sources.marketplace.trend import (
    load_stats,
    review_trend_signal,
    save_stats,
)

TODAY = date(2026, 8, 31)


def test_crossing_thresholds_emits_candidate():
    prev = {"count": 10, "avg_rating": 4.2}
    curr = {"count": 30, "avg_rating": 3.5}
    cand = review_trend_signal("jira", "g2", prev, curr, domain="acme.com", today=TODAY)
    assert cand is not None
    assert cand.signal_type == "marketplace_review_trend"
    assert cand.natural_key == "rvtrend:g2:jira:2026-08-31"
    assert cand.evidence_data["prev"]["count"] == 10
    assert cand.evidence_data["current"]["count"] == 30
    assert cand.evidence_data["count_delta"] == 20
    assert abs(cand.evidence_data["rating_delta"] - (-0.7)) < 1e-9
    assert cand.evidence_data["product_slug"] == "jira"
    assert cand.evidence_data["source"] == "g2"


def test_below_threshold_returns_none():
    prev = {"count": 10, "avg_rating": 4.2}
    curr = {"count": 12, "avg_rating": 4.0}  # both deltas below thresholds
    assert (
        review_trend_signal("jira", "g2", prev, curr, domain="acme.com", today=TODAY)
        is None
    )


def test_no_previous_returns_none():
    curr = {"count": 30, "avg_rating": 3.5}
    assert (
        review_trend_signal("jira", "g2", None, curr, domain="acme.com", today=TODAY)
        is None
    )


def test_count_threshold_alone_crosses():
    prev = {"count": 10, "avg_rating": 4.2}
    curr = {"count": 16, "avg_rating": 4.2}
    cand = review_trend_signal("jira", "g2", prev, curr, domain="acme.com", today=TODAY)
    assert cand is not None
    assert cand.evidence_data["count_delta"] == 6
    assert cand.evidence_data["rating_delta"] == 0.0


def test_custom_thresholds():
    prev = {"count": 10, "avg_rating": 4.2}
    curr = {"count": 12, "avg_rating": 4.6}
    cand = review_trend_signal(
        "jira", "capterra", prev, curr,
        domain="acme.com", today=TODAY,
        min_count_delta=2, min_rating_delta=0.3,
    )
    assert cand is not None
    assert cand.natural_key.startswith("rvtrend:capterra:jira:2026-08-31")


def test_today_accepts_string():
    prev = {"count": 1, "avg_rating": 1.0}
    curr = {"count": 99, "avg_rating": 5.0}
    cand = review_trend_signal("s", "g2", prev, curr, domain="d", today="2026-08-31")
    assert cand.natural_key == "rvtrend:g2:s:2026-08-31"


def test_stats_roundtrip(tmp_path):
    path = tmp_path / "stats.json"
    save_stats({"g2:jira": {"count": 10, "avg_rating": 4.2}}, path)
    assert load_stats(path) == {"g2:jira": {"count": 10, "avg_rating": 4.2}}


def test_load_stats_missing_or_corrupt(tmp_path):
    assert load_stats(tmp_path / "nope.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert load_stats(bad) == {}
