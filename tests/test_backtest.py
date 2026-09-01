"""Tests for play hit-rate backtesting (P2 Task 16).

Covers: outcome recording with idempotent upsert, per-play hit-rate math,
calibration feed gating on min_samples, and migration v5 (play_outcomes).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.db import Database, LATEST_VERSION  # noqa: E402
from signals.backtest import (  # noqa: E402
    feed_calibration,
    play_hit_rates,
    record_outcome,
)


def _make_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "backtest.db")
    return db


def _insert_signal(db: Database, domain: str, signal_type: str, source: str = "news_rss") -> str:
    signal_id = f"sig_{domain}_{signal_type}_{source}"
    db.execute(
        """
        INSERT OR IGNORE INTO signals (
            signal_id, domain, signal_type, source, observed_at,
            category, origin, catalyst, polarity, first_seen_at, last_seen_at
        )
        VALUES (?, ?, ?, ?, ?, 'financial', 'external', 'primary', 'positive', ?, ?)
        """,
        (signal_id, domain, signal_type, source, "2026-08-30T00:00:00Z",
         "2026-08-30T00:00:00Z", "2026-08-30T00:00:00Z"),
    )
    return signal_id


def _insert_assignment(db: Database, domain: str, play_id: str, signal_id: str) -> None:
    db.execute(
        """
        INSERT OR IGNORE INTO play_assignments (domain, play_id, signal_id)
        VALUES (?, ?, ?)
        """,
        (domain, play_id, signal_id),
    )


# ── record_outcome: insert + idempotent upsert ────────────────────────────


def test_record_outcome_inserts_then_updates_same_pk(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    try:
        record_outcome(db, "acme.com", "play_funding", "hit")
        row = db.one(
            "SELECT * FROM play_outcomes WHERE domain=? AND play_id=?",
            ("acme.com", "play_funding"),
        )
        assert row is not None
        assert row["outcome"] == "hit"

        # Re-record the same (domain, play_id): still a single row, outcome updated.
        record_outcome(db, "acme.com", "play_funding", "miss")
        rows = db.query(
            "SELECT * FROM play_outcomes WHERE domain=? AND play_id=?",
            ("acme.com", "play_funding"),
        )
        assert len(rows) == 1
        assert rows[0]["outcome"] == "miss"
    finally:
        db.close()


# ── play_hit_rates: rate math ─────────────────────────────────────────────


def test_hit_rate_math_three_sent_one_hit(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    try:
        for domain, outcome in (
            ("a.com", "hit"),
            ("b.com", "miss"),
            ("c.com", "miss"),
        ):
            sig = _insert_signal(db, domain, "funding")
            _insert_assignment(db, domain, "play_v", sig)
            record_outcome(db, domain, "play_v", outcome)
        # a second play with no outcomes at all (sent=2, decided none)
        db.execute(
            "INSERT INTO play_assignments (domain, play_id, signal_id) VALUES (?, ?, ?)",
            ("d.com", "play_empty", "sig_x"),
        )
        db.execute(
            "INSERT INTO play_assignments (domain, play_id, signal_id) VALUES (?, ?, ?)",
            ("e.com", "play_empty", "sig_y"),
        )

        rates = play_hit_rates(db)
        r = rates["play_v"]
        assert r["sent"] == 3
        assert r["hit"] == 1
        assert abs(r["rate"] - 1 / 3) < 1e-9
        # play with zero outcomes → sent counts assignments, hit=0, rate 0.0
        empty = rates["play_empty"]
        assert empty["sent"] == 2
        assert empty["hit"] == 0
        assert empty["rate"] == 0.0
    finally:
        db.close()


# ── feed_calibration: samples < min_samples stays no-op ───────────────────


def test_feed_calibration_below_min_samples_is_noop(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    try:
        sig = _insert_signal(db, "acme.com", "funding", source="news_rss")
        for i in range(5):  # 5 assignments, all decided
            domain = f"d{i}.com"
            _insert_assignment(db, domain, f"play_{i}", sig)
            record_outcome(db, domain, f"play_{i}", "hit" if i < 3 else "miss")

        feed_calibration(db)  # default min_samples=30 → no-op
        assert db.one("SELECT * FROM calibration WHERE source='news_rss'") is None

        # Explicit low min_samples → row written with samples=5 hits=3.
        feed_calibration(db, min_samples=1)
        row = db.one("SELECT * FROM calibration WHERE source='news_rss'")
        assert row is not None
        assert row["signal_type"] == "funding"
        assert row["samples"] == 5
        assert row["hits"] == 3
    finally:
        db.close()


def test_feed_calibration_writes_when_threshold_met(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    try:
        sig = _insert_signal(db, "acme.com", "hiring", source="ats_greenhouse")
        for i in range(30):
            domain = f"d{i}.com"
            _insert_assignment(db, domain, f"play_{i}", sig)
            record_outcome(db, domain, f"play_{i}", "hit" if i < 10 else "miss")

        feed_calibration(db)  # default min_samples=30 → exactly 30 samples → written
        row = db.one(
            "SELECT * FROM calibration WHERE source='ats_greenhouse' AND signal_type='hiring'"
        )
        assert row is not None
        assert row["samples"] == 30
        assert row["hits"] == 10
    finally:
        db.close()


def test_feed_calibration_upserts_on_replay(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    try:
        sig = _insert_signal(db, "acme.com", "funding", source="news_rss")
        for i in range(31):
            domain = f"d{i}.com"
            _insert_assignment(db, domain, f"play_{i}", sig)
            record_outcome(db, domain, f"play_{i}", "hit")

        feed_calibration(db)
        feed_calibration(db)  # idempotent: same PK, same values
        rows = db.query(
            "SELECT * FROM calibration WHERE source='news_rss' AND signal_type='funding'"
        )
        assert len(rows) == 1
        assert rows[0]["samples"] == 31
        assert rows[0]["hits"] == 31
    finally:
        db.close()


# ── migration v5: play_outcomes table exists, user_version == 5 ───────────


def test_migration_v5_play_outcomes_table(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    try:
        version = db.one("PRAGMA user_version")["user_version"]
        assert version == LATEST_VERSION
        assert version >= 5  # v5 created play_outcomes; later migrations may follow
        cols = db.table_columns("play_outcomes")
        assert {"domain", "play_id", "outcome", "decided_at"} <= cols
        # PK is (domain, play_id) — inserting the same pair twice must conflict.
        with db._lock:
            with pytest.raises(sqlite3.IntegrityError):
                db.conn.execute(
                    "INSERT INTO play_outcomes (domain, play_id, outcome) VALUES (?, ?, ?)",
                    ("acme.com", "p1", "hit"),
                )
                db.conn.execute(
                    "INSERT INTO play_outcomes (domain, play_id, outcome) VALUES (?, ?, ?)",
                    ("acme.com", "p1", "hit"),
                )
    finally:
        db.close()


import pytest  # noqa: E402
