"""Prune: data-retention enforcement over fetch_log / documents / runs + raw files."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from src.core.db import Database, prune_all


@pytest.fixture()
def db(tmp_path):
    return Database(tmp_path / "signals.db")


def _iso_old(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _seed(db: Database, *, old_days: int = 120, new_days: int = 1) -> None:
    old, new = _iso_old(old_days), _iso_old(new_days)
    db.execute(
        "INSERT INTO fetch_log (run_id, source, domain, url, status, at) VALUES"
        " ('r-old', 'g2', 'old.com', 'u', 200, ?)",
        (old,),
    )
    db.execute(
        "INSERT INTO fetch_log (run_id, source, domain, url, status, at) VALUES"
        " ('r-new', 'g2', 'new.com', 'u', 200, ?)",
        (new,),
    )
    db.execute(
        "INSERT INTO documents (doc_id, source, domain, url, fetched_at)"
        " VALUES ('doc-old', 'g2', 'old.com', 'u', ?)",
        (old,),
    )
    db.execute(
        "INSERT INTO documents (doc_id, source, domain, url, fetched_at)"
        " VALUES ('doc-new', 'g2', 'new.com', 'u', ?)",
        (new,),
    )
    db.execute(
        "INSERT INTO runs (run_id, stage, started_at, status) VALUES"
        " ('r-old', 'harvest', ?, 'done')",
        (old,),
    )
    db.execute(
        "INSERT INTO runs (run_id, stage, started_at, status) VALUES"
        " ('r-new', 'harvest', ?, 'done')",
        (new,),
    )


def _counts(db: Database) -> dict:
    return {
        t: db.one(f"SELECT COUNT(*) AS n FROM {t}")["n"]
        for t in ("fetch_log", "documents", "runs")
    }


def test_prune_all_deletes_old_keeps_new(db):
    _seed(db)
    out = prune_all(db, keep_days=30)
    assert out["fetch_log"] == 1
    assert out["documents"] == 1
    assert out["runs"] == 1
    assert out["raw_files"] == 0
    c = _counts(db)
    assert c == {"fetch_log": 1, "documents": 1, "runs": 1}


def test_prune_all_is_idempotent(db):
    _seed(db)
    prune_all(db, keep_days=30)
    again = prune_all(db, keep_days=30)
    assert again == {"fetch_log": 0, "documents": 0, "runs": 0, "raw_files": 0}


def test_prune_all_uses_real_timestamp_columns(db):
    """Cutoff comparison runs against each table's actual timestamp column."""
    _seed(db)
    prune_all(db, keep_days=30)
    for col, tbl in (("at", "fetch_log"), ("fetched_at", "documents"), ("started_at", "runs")):
        assert db.one(f"SELECT MIN({col}) AS m FROM {tbl}")["m"] >= _iso_old(30)


def test_prune_all_calls_raw_store_prune(db):
    _seed(db)
    raw = MagicMock()
    raw.prune.return_value = 4
    out = prune_all(db, keep_days=30, raw_store=raw)
    raw.prune.assert_called_once_with(30)
    assert out["raw_files"] == 4
    # RawStore.prune deletes the documents rows itself; prune_all must not
    # double-delete, so documents count comes from the store.
    assert out["documents"] == 4


def test_prune_all_without_raw_store_deletes_document_rows(db):
    _seed(db)
    out = prune_all(db, keep_days=30)
    assert out["documents"] == 1


def test_prune_all_checkpoints_and_analyzes(db):
    _seed(db)
    # Should not raise; WAL checkpoint + ANALYZE run against the real connection.
    prune_all(db, keep_days=30)
    assert db.one("PRAGMA integrity_check")["integrity_check"] == "ok"


def test_cli_prune(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from click.testing import CliRunner

    from src.cli import main

    cfg = SimpleNamespace(
        storage=SimpleNamespace(db_path=str(tmp_path / "signals.db"), raw_dir=str(tmp_path / "raw")),
    )
    monkeypatch.setattr("src.cli.Config.load", lambda *a, **k: cfg)
    res = CliRunner().invoke(main, ["prune", "--keep-days", "30"])
    assert res.exit_code == 0, res.output
    assert "fetch_log" in res.output
