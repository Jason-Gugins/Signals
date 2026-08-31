"""Tests for the G2/empty-slug EmptyLog state file (RouteState-style)."""

from __future__ import annotations

import json

from src.sources.marketplace.empty_log import EmptyLog


def make(tmp_path, clock=lambda: 1000.0):
    return EmptyLog(tmp_path / "empty_slugs.json", clock=clock)


def test_record_empty_then_reviews_resets(tmp_path):
    log = make(tmp_path)
    log.record_empty("jira", "g2")
    log.record_empty("jira", "g2")
    e = log.entry("jira", "g2")
    assert e["cycles"] == 2
    assert "first_empty" in e
    log.record_reviews("jira", "g2")
    assert log.entry("jira", "g2") == {}


def test_backoff_after_threshold_consecutive_empties(tmp_path):
    log = make(tmp_path)
    assert not log.is_in_backoff("jira", "g2")
    log.record_empty("jira", "g2")
    log.record_empty("jira", "g2")
    assert not log.is_in_backoff("jira", "g2")  # 2 < 3
    log.record_empty("jira", "g2")
    assert log.is_in_backoff("jira", "g2")  # 3 consecutive empties
    log.record_reviews("jira", "g2")
    assert not log.is_in_backoff("jira", "g2")


def test_slugs_and_sources_are_independent(tmp_path):
    log = make(tmp_path)
    log.record_empty("jira", "g2")
    log.record_empty("jira", "g2")
    log.record_empty("jira", "g2")
    assert log.is_in_backoff("jira", "g2")
    assert not log.is_in_backoff("jira", "capterra")
    assert not log.is_in_backoff("slack", "g2")


def test_custom_threshold(tmp_path):
    log = make(tmp_path)
    log.record_empty("jira", "g2")
    log.record_empty("jira", "g2")
    assert log.is_in_backoff("jira", "g2", threshold_cycles=2)


def test_corrupt_file_tolerated(tmp_path):
    path = tmp_path / "empty_slugs.json"
    path.write_text("{corrupt!!", encoding="utf-8")
    log = EmptyLog(path, clock=lambda: 1000.0)
    for _ in range(3):
        log.record_empty("jira", "g2")
    assert log.is_in_backoff("jira", "g2")


def test_missing_file_tolerated(tmp_path):
    log = make(tmp_path)
    assert log.entry("jira", "g2") == {}
    assert not log.is_in_backoff("jira", "g2")


def test_clock_injection_sets_first_empty(tmp_path):
    log = EmptyLog(tmp_path / "e.json", clock=lambda: 1756600000.0)
    log.record_empty("jira", "g2")
    data = json.loads((tmp_path / "e.json").read_text(encoding="utf-8"))
    assert data["g2:jira"]["first_empty"] == "2025-08-30"


def test_state_persists_across_instances(tmp_path):
    log = make(tmp_path)
    log.record_empty("jira", "g2")
    log.record_empty("jira", "g2")
    log.record_empty("jira", "g2")
    again = make(tmp_path)
    assert again.is_in_backoff("jira", "g2")


def test_save_is_atomic_no_tmp_left_behind(tmp_path):
    log = make(tmp_path)
    log.record_empty("jira", "g2")
    data = json.loads((tmp_path / "empty_slugs.json").read_text(encoding="utf-8"))
    assert data["g2:jira"]["cycles"] == 1
    assert not (tmp_path / "empty_slugs.json.tmp").exists()
