"""Task 22: per-source health report from fetch_log."""

from __future__ import annotations

from src.core.db import Database
from src.pipeline.health_report import source_health


def _seed(db: Database, rows: list[dict]) -> None:
    for r in rows:
        db.execute(
            "INSERT INTO fetch_log (run_id, source, domain, url, status, elapsed_ms,"
            " bytes, cached, error, at, error_class) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                r.get("run_id"),
                r["source"],
                r.get("domain"),
                r.get("url"),
                r.get("status"),
                r.get("elapsed_ms"),
                r.get("bytes"),
                r.get("cached", 0),
                r.get("error"),
                r["at"],
                r.get("error_class"),
            ),
        )
    db.conn.commit()


def test_counts_and_rates(tmp_path):
    db = Database(tmp_path / "h.db")
    _seed(
        db,
        [
            {"source": "gh", "status": 200, "cached": 1, "at": "2026-08-30 10:00:00"},
            {"source": "gh", "status": 200, "cached": 0, "at": "2026-08-30 09:00:00"},
            {"source": "gh", "status": 403, "error": "forbidden", "error_class": "challenge", "at": "2026-08-30 08:00:00"},
            {"source": "hn", "status": 200, "at": "2026-08-30 07:00:00"},
            {"source": "hn", "status": None, "error": "timed out", "error_class": "timeout", "at": "2026-08-30 06:00:00"},
        ],
    )
    rows = {r["source"]: r for r in source_health(db, since_hours=24 * 365)}
    gh = rows["gh"]
    assert gh["fetched"] == 3
    assert gh["cached"] == 1
    assert gh["failed"] == 1
    assert gh["last_success"] == "2026-08-30 10:00:00"
    assert gh["success_rate"] == round(1 - 1 / 3, 2)
    assert gh["error_class"] == {"challenge": 1}
    hn = rows["hn"]
    assert hn["fetched"] == 2
    assert hn["cached"] == 0
    assert hn["failed"] == 1
    assert hn["last_success"] == "2026-08-30 07:00:00"
    assert hn["error_class"] == {"timeout": 1}


def test_empty_log(tmp_path):
    db = Database(tmp_path / "h.db")
    assert source_health(db) == []


def test_window_filters_old_rows(tmp_path):
    db = Database(tmp_path / "h.db")
    _seed(
        db,
        [
            {"source": "old", "status": 500, "error": "boom", "at": "2020-01-01 00:00:00"},
            {"source": "new", "status": 200, "at": "2026-08-30 10:00:00"},
        ],
    )
    rows = source_health(db, since_hours=168)
    assert [r["source"] for r in rows] == ["new"]


def test_challenge_class_in_histogram(tmp_path):
    db = Database(tmp_path / "h.db")
    _seed(
        db,
        [
            {"source": "pn", "status": 403, "error": "px challenge", "error_class": "challenge", "at": "2026-08-30 10:00:00"},
            {"source": "pn", "status": 403, "error": "px challenge", "error_class": "challenge", "at": "2026-08-30 11:00:00"},
        ],
    )
    rows = source_health(db)
    assert len(rows) == 1
    assert rows[0]["error_class"]["challenge"] == 2
    assert rows[0]["success_rate"] == 0.0
