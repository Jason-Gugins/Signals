"""Tests for the SQLite database layer."""

from __future__ import annotations

import src.core.db as dbmod
from src.core.db import Database, NEW_COLUMNS


EXPECTED_TABLES = {
    "accounts",
    "account_aliases",
    "contacts",
    "signals",
    "score_history",
    "play_assignments",
    "documents",
    "source_cursors",
    "fetch_log",
    "runs",
    "watchlist",
}


def test_fresh_db_creates_all_11_tables(tmp_path):
    db = Database(tmp_path / "signals.db")
    names = {
        row["name"]
        for row in db.query("SELECT name FROM sqlite_master WHERE type='table'")
        if not row["name"].startswith("sqlite_")
    }
    assert EXPECTED_TABLES.issubset(names)
    assert len(EXPECTED_TABLES) == 11
    db.close()


def test_migrate_twice_is_noop(tmp_path):
    db = Database(tmp_path / "signals.db")
    db._migrate()
    db._migrate()
    rows = db.query("SELECT domain FROM accounts")
    assert rows == []
    db.close()


def test_new_columns_adds_column_and_preserves_rows(tmp_path, monkeypatch):
    path = tmp_path / "signals.db"
    db = Database(path)
    db.execute(
        "INSERT INTO accounts(domain, name) VALUES (?, ?)",
        ("acme.com", "Acme"),
    )
    db.close()

    extra = {**NEW_COLUMNS, "accounts": {**NEW_COLUMNS.get("accounts", {}), "foo_col": "TEXT"}}
    monkeypatch.setattr(dbmod, "NEW_COLUMNS", extra)

    db2 = Database(path)
    cols = db2.table_columns("accounts")
    assert "foo_col" in cols
    row = db2.one("SELECT domain, name, foo_col FROM accounts WHERE domain = ?", ("acme.com",))
    assert row["domain"] == "acme.com"
    assert row["name"] == "Acme"
    assert row["foo_col"] is None
    db2.close()


def test_upsert_inserts_then_coalesces_without_nulling(tmp_path):
    db = Database(tmp_path / "signals.db")
    db.upsert("accounts", {"domain": "acme.com", "name": "Acme", "score": 10.0}, pk="domain")
    db.upsert("accounts", {"domain": "acme.com", "name": None, "industry": "Software"}, pk="domain")
    row = db.one("SELECT name, industry, score FROM accounts WHERE domain = ?", ("acme.com",))
    assert row["name"] == "Acme"
    assert row["industry"] == "Software"
    assert row["score"] == 10.0
    db.close()


def test_upsert_overwrite_replaces_non_null(tmp_path):
    db = Database(tmp_path / "signals.db")
    db.upsert("accounts", {"domain": "acme.com", "score": 10.0}, pk="domain")
    db.upsert(
        "accounts",
        {"domain": "acme.com", "score": 42.0},
        pk="domain",
        overwrite={"score"},
    )
    row = db.one("SELECT score FROM accounts WHERE domain = ?", ("acme.com",))
    assert row["score"] == 42.0
    db.close()


def test_query_returns_dicts(tmp_path):
    db = Database(tmp_path / "signals.db")
    db.execute("INSERT INTO accounts(domain, name) VALUES (?, ?)", ("acme.com", "Acme"))
    rows = db.query("SELECT domain, name FROM accounts")
    assert rows == [{"domain": "acme.com", "name": "Acme"}]
    assert isinstance(rows[0], dict)
    db.close()


def test_wal_enabled(tmp_path):
    db = Database(tmp_path / "signals.db")
    mode = db.one("PRAGMA journal_mode")
    assert list(mode.values())[0].lower() == "wal"
    db.close()
