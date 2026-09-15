"""Wave B: stale 'running' runs rows — detect, surface, repair."""

import pytest
from datetime import datetime, timedelta, timezone

from src.core.db import Database

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db(tmp_path):
    return Database(tmp_path / "signals.db")


def _run(db, run_id, started_at, status="running"):
    db.execute(
        "INSERT INTO runs(run_id, stage, started_at, status) VALUES (?,?,?,?)",
        (run_id, "collect", started_at, status),
    )


def _at(hours: int = 0, *, seconds: int = 0) -> str:
    return (NOW - timedelta(hours=hours, seconds=seconds)).replace(microsecond=0).isoformat()


def test_fresh_running_row_is_not_stale(db):
    from src.pipeline.health import stale_running_runs

    _run(db, "fresh", _at(seconds=300))
    assert stale_running_runs(db, now=NOW) == []


def test_two_day_old_running_row_is_stale(db):
    from src.pipeline.health import stale_running_runs

    _run(db, "old", _at(hours=48))
    rows = stale_running_runs(db, now=NOW)
    assert len(rows) == 1
    assert rows[0]["run_id"] == "old"


def test_closed_rows_are_never_stale(db):
    from src.pipeline.health import stale_running_runs

    _run(db, "done", _at(hours=48), status="completed")
    assert stale_running_runs(db, now=NOW) == []


def test_finalize_marks_failed_with_a_note(db):
    from src.pipeline.health import finalize_stale_runs

    _run(db, "old", _at(hours=48))
    assert finalize_stale_runs(db, now=NOW) == 1
    row = db.one("SELECT status, notes, finished_at FROM runs WHERE run_id='old'")
    assert row["status"] == "failed"
    assert row["notes"]
    assert row["finished_at"]


def test_finalize_is_idempotent(db):
    from src.pipeline.health import finalize_stale_runs

    _run(db, "old", _at(hours=48))
    assert finalize_stale_runs(db, now=NOW) == 1
    before = db.one("SELECT finished_at FROM runs WHERE run_id='old'")["finished_at"]
    assert finalize_stale_runs(db, now=NOW) == 0
    after = db.one("SELECT finished_at FROM runs WHERE run_id='old'")["finished_at"]
    assert after == before


def test_cutoff_boundary(db):
    from src.pipeline.health import stale_running_runs

    # Exactly max_age_hours before NOW -> not stale (strict <).
    _run(db, "boundary", (NOW - timedelta(hours=6)).replace(microsecond=0).isoformat())
    # One second older than the cutoff -> stale.
    _run(db, "older", (NOW - timedelta(hours=6, seconds=1)).replace(microsecond=0).isoformat())
    rows = stale_running_runs(db, now=NOW)
    assert [r["run_id"] for r in rows] == ["older"]
