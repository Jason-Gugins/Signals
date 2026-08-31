"""Tests for the versioned DB migration system (Task 8).

Covers: fresh DB gets latest user_version; a v0 DB missing a column is
migrated forward on reopen; a corrupt file raises a clear error; reopen is
idempotent; integrity_check runs on startup.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core import db as db_mod  # noqa: E402
from core.db import Database  # noqa: E402

LATEST = max(v for v, _, _ in db_mod.MIGRATIONS)


def _user_version(db: Database) -> int:
    return db.one("PRAGMA user_version")["user_version"]


def test_fresh_db_gets_latest_version(tmp_path: Path) -> None:
    db = Database(tmp_path / "fresh.db")
    try:
        assert _user_version(db) == LATEST
        assert LATEST >= 1
    finally:
        db.close()


def test_v0_db_missing_column_is_migrated_on_reopen(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    db = Database(path)
    # Simulate a pre-migration v0 DB: drop the source column from g2_reviews
    # and roll user_version back to 0.
    db.execute("ALTER TABLE g2_reviews DROP COLUMN source")
    db.execute(f"PRAGMA user_version = 0")
    cols_before = db.table_columns("g2_reviews")
    assert "source" not in cols_before
    db.close()

    # Reopen: migration must re-add the column and advance the version.
    db2 = Database(path)
    try:
        cols_after = db2.table_columns("g2_reviews")
        assert "source" in cols_after
        assert _user_version(db2) == LATEST
        # The other NEW_COLUMNS entries survived too.
        for col in ("nps_score", "helpful_votes"):
            assert col in cols_after
    finally:
        db2.close()


def test_v2_db_gets_source_cursors_error_class(tmp_path: Path) -> None:
    """Legacy v2 DBs predate the fetch error taxonomy: reopening must add
    source_cursors.error_class via the additive NEW_COLUMNS pass."""
    path = tmp_path / "v2legacy.db"
    db = Database(path)
    # Simulate a v2-era source_cursors table without the error_class column.
    db.execute("ALTER TABLE source_cursors RENAME TO source_cursors_old")
    db.execute(
        """
        CREATE TABLE source_cursors (
            source TEXT NOT NULL,
            key TEXT NOT NULL,
            cursor TEXT,
            etag TEXT,
            last_modified TEXT,
            last_run_at TEXT,
            next_due_at TEXT,
            fail_count INTEGER DEFAULT 0,
            last_error TEXT,
            PRIMARY KEY (source, key)
        )
        """
    )
    db.execute("DROP TABLE source_cursors_old")
    db.execute("PRAGMA user_version = 2")
    assert "error_class" not in db.table_columns("source_cursors")
    db.close()

    db2 = Database(path)
    try:
        cols = db2.table_columns("source_cursors")
        assert "error_class" in cols
        assert _user_version(db2) == LATEST
        # The column must be usable: stamp a classified error class.
        db2.upsert(
            "source_cursors",
            {"source": "news_rss", "key": "acme.com", "error_class": "auth"},
            pk=("source", "key"),
        )
        row = db2.one("SELECT error_class FROM source_cursors WHERE source='news_rss'")
        assert row["error_class"] == "auth"
    finally:
        db2.close()


def test_corrupt_db_raises_clear_error(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.db"
    path.write_bytes(b"this is definitely not a sqlite database" * 64)
    with pytest.raises(RuntimeError) as excinfo:
        Database(path)
    msg = str(excinfo.value).lower()
    assert "corrupt" in msg or "not a database" in msg


def test_idempotent_reopen(tmp_path: Path) -> None:
    path = tmp_path / "idem.db"
    db = Database(path)
    version_first = _user_version(db)
    cols_first = db.table_columns("g2_reviews")
    db.close()

    db2 = Database(path)
    try:
        assert _user_version(db2) == version_first
        assert db2.table_columns("g2_reviews") == cols_first
    finally:
        db2.close()


def test_integrity_check_runs_on_startup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    messages: list[str] = []
    sink_id = db_mod.logger.add(messages.append, level="DEBUG")

    path = tmp_path / "ic.db"
    db = Database(path)
    try:
        db_mod.logger.remove(sink_id)
    except ValueError:
        pass
    assert any("integrity" in m.lower() for m in messages)
