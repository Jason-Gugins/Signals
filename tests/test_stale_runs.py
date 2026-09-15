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


def test_status_report_exposes_the_stale_count(db):
    from src.pipeline.health import status_report

    _run(db, "old", _at(hours=48))
    assert status_report(db, taxonomy=None)["runs"]["stale_running"] == 1

    fresh_db = Database(db.db_path.parent / "fresh.db")
    _run(fresh_db, "fresh", _at(seconds=300))
    assert status_report(fresh_db, taxonomy=None)["runs"]["stale_running"] == 0


def test_render_status_prints_the_stale_line(db):
    from src.pipeline.health import render_status, status_report

    _run(db, "old", _at(hours=48))
    text = render_status(status_report(db, taxonomy=None))
    assert "stale" in text
    assert "prune" in text

    fresh_db = Database(db.db_path.parent / "fresh.db")
    _run(fresh_db, "fresh", _at(seconds=300))
    clean = render_status(status_report(fresh_db, taxonomy=None))
    assert "stale" not in clean
    assert "prune" not in clean


def test_doctor_reports_a_warn_row(tmp_path):
    import unittest.mock as mock

    from src.core.config import Config
    from src.pipeline.health import doctor

    cfg = Config()
    cfg.contact_email = "x@y.z"
    cfg.config_dir = str(tmp_path / "cfg")
    cfg.storage.raw_dir = str(tmp_path / "raw")
    cfg.storage.export_dir = str(tmp_path / "ex")
    cfg.storage.briefs_dir = str(tmp_path / "br")
    cfg.external_dbs.linkedin_db = str(tmp_path / "m1.db")
    cfg.external_dbs.repvue_db = str(tmp_path / "m2.db")

    real_load = Config.load_yaml

    def fake_load(self, name):
        return real_load(self, name)

    db = Database(tmp_path / "s.db")
    _run(db, "old", _at(hours=48))

    with mock.patch.object(Config, "load_yaml", fake_load):
        rows = doctor(cfg, db, check_network=False)

    stale_rows = [(n, s, d) for n, s, d in rows if n == "stale_runs"]
    assert len(stale_rows) == 1
    name, status, detail = stale_rows[0]
    assert status == "WARN"
    assert "prune" in detail
    assert all(s in {"OK", "WARN", "FAIL"} for _, s, _ in rows)


def _cli_prune(tmp_path, monkeypatch, argv):
    from types import SimpleNamespace

    from click.testing import CliRunner

    from src.cli import main

    cfg = SimpleNamespace(
        storage=SimpleNamespace(db_path=str(tmp_path / "signals.db"), raw_dir=str(tmp_path / "raw")),
    )
    monkeypatch.setattr("src.cli.Config.load", lambda *a, **k: cfg)
    res = CliRunner().invoke(main, argv)
    return res, cfg


def _recent(*, hours: int = 0, minutes: int = 0):
    """ISO timestamp relative to real now — the CLI cannot inject `now`."""
    return (datetime.now(timezone.utc) - timedelta(hours=hours, minutes=minutes)).replace(microsecond=0).isoformat()


def test_cli_prune_finalizes_stale_rows(tmp_path, monkeypatch):
    db = Database(tmp_path / "signals.db")
    _run(db, "old", _recent(hours=48))
    _run(db, "fresh", _recent(minutes=5))

    res, cfg = _cli_prune(tmp_path, monkeypatch, ["prune", "--keep-days", "30"])
    assert res.exit_code == 0, res.output
    assert "stale_runs_finalized=1" in res.output

    check = Database(cfg.storage.db_path)
    old = check.one("SELECT status, notes, finished_at FROM runs WHERE run_id='old'")
    assert old["status"] == "failed"
    assert old["notes"]
    assert old["finished_at"]
    fresh = check.one("SELECT status FROM runs WHERE run_id='fresh'")
    assert fresh["status"] == "running"


def test_prune_stale_run_hours_option_is_honoured(tmp_path, monkeypatch):
    db = Database(tmp_path / "signals.db")
    _run(db, "recent", _recent(hours=12))

    res, cfg = _cli_prune(
        tmp_path, monkeypatch, ["prune", "--keep-days", "30", "--stale-run-hours", "48"]
    )
    assert res.exit_code == 0, res.output
    assert "stale_runs_finalized=0" in res.output

    check = Database(cfg.storage.db_path)
    assert check.one("SELECT status FROM runs WHERE run_id='recent'")["status"] == "running"


