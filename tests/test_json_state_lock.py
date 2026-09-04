"""exclusive_lock: fail-open file lock for shared-JSON read-modify-write.

The runner mutates three shared JSON state files (marketplace stats,
jobsignals stats, the empty-slug log) via load -> mutate -> save cycles.
A manual ``collect`` racing the watch tick can lose updates (last writer
wins). These tests pin the :func:`src.core.filelock.exclusive_lock`
context manager that serializes those cycles: acquire/release, stale-lock
breaking, contended-timeout fail-open, and no-lost-update under the lock.
"""

from __future__ import annotations

import os
import time

from loguru import logger

from src.core.filelock import exclusive_lock


def test_acquire_creates_lock_release_removes_it(tmp_path):
    target = tmp_path / "stats.json"
    lock_file = tmp_path / "stats.json.lock"
    with exclusive_lock(target) as acquired:
        assert acquired is True
        assert lock_file.exists()
        # the lockfile holds the owning pid (SingleFlight convention)
        assert int(lock_file.read_text(encoding="utf-8").strip()) == os.getpid()
    assert not lock_file.exists()


def test_dead_pid_lock_is_stale_and_broken(tmp_path):
    target = tmp_path / "stats.json"
    lock_file = tmp_path / "stats.json.lock"
    lock_file.write_text("999999999", encoding="utf-8")  # pid cannot exist
    with exclusive_lock(target) as acquired:
        assert acquired is True
        assert int(lock_file.read_text(encoding="utf-8").strip()) == os.getpid()
    assert not lock_file.exists()


def test_stale_lock_broken_and_reacquired(tmp_path):
    target = tmp_path / "stats.json"
    lock_file = tmp_path / "stats.json.lock"
    lock_file.write_text(str(os.getpid()), encoding="utf-8")  # our pid: alive
    old = time.time() - 3600  # far older than the default stale_s=30
    os.utime(lock_file, (old, old))
    with exclusive_lock(target) as acquired:
        assert acquired is True
        assert int(lock_file.read_text(encoding="utf-8").strip()) == os.getpid()
    assert not lock_file.exists()


def test_contended_lock_times_out_yields_false(tmp_path):
    target = tmp_path / "stats.json"
    lock_file = tmp_path / "stats.json.lock"
    with exclusive_lock(target) as first:
        assert first is True
        with exclusive_lock(target, timeout_s=0.1, poll_s=0.02) as second:
            assert second is False
        # the failed acquirer must not have disturbed the holder's lock
        assert lock_file.exists()
    assert not lock_file.exists()


def test_locked_trend_rmw_no_lost_update(tmp_path):
    """Two serialized load->mutate->save cycles both survive (no clobber)."""
    from src.sources.marketplace.trend import (
        load_stats,
        save_stats,
        stats_key,
    )

    stats_path = tmp_path / "stats.json"
    with exclusive_lock(stats_path) as acquired:
        assert acquired is True
        state = load_stats(stats_path)
        state[stats_key("capterra", "19319/JIRA")] = {"count": 3, "avg_rating": 4.5}
        save_stats(state, stats_path)
    with exclusive_lock(stats_path) as acquired:
        state = load_stats(stats_path)
        state[stats_key("g2", "sierra")] = {"count": 7, "avg_rating": 4.0}
        save_stats(state, stats_path)
    final = load_stats(stats_path)
    assert final[stats_key("capterra", "19319/JIRA")]["count"] == 3
    assert final[stats_key("g2", "sierra")]["count"] == 7


def test_locked_empty_log_double_record_no_lost_update(tmp_path):
    """Two EmptyLog instances recording under the lock: both bumps land."""
    from src.sources.marketplace.empty_log import EmptyLog

    path = tmp_path / "empty_slugs.json"
    log_a = EmptyLog(path, clock=lambda: 1000.0)
    with exclusive_lock(path):
        log_a.record_empty("19319/JIRA", "capterra")
    log_b = EmptyLog(path, clock=lambda: 1000.0)  # fresh load, like a new process
    with exclusive_lock(path):
        log_b.record_empty("19319/JIRA", "capterra")
    entry = log_b.entry("19319/JIRA", "capterra")
    assert entry["cycles"] == 2
    assert entry["first_empty"]


def test_fail_open_when_foreign_lock_held(tmp_path):
    """A held foreign lock + tiny timeout: the guarded update still completes
    (fail-open) and a warning is logged."""
    from src.sources.marketplace.empty_log import EmptyLog

    path = tmp_path / "empty_slugs.json"
    lock_file = tmp_path / "empty_slugs.json.lock"
    lock_file.write_text(str(os.getpid()), encoding="utf-8")  # alive pid, fresh
    log = EmptyLog(path, clock=lambda: 1000.0)

    messages: list = []
    handler_id = logger.add(messages.append, level="WARNING")
    try:
        with exclusive_lock(path, timeout_s=0.05, poll_s=0.01) as acquired:
            assert acquired is False
            log.record_empty("19319/JIRA", "capterra")
    finally:
        logger.remove(handler_id)

    assert log.entry("19319/JIRA", "capterra")["cycles"] == 1
    assert any(m.record["level"].name == "WARNING" for m in messages)
    # own-lock-only release: the foreign lock survives the failed acquirer
    assert lock_file.exists()
