"""Tests for the SQLite database layer."""

from __future__ import annotations

import json

import src.core.db as dbmod
from src.core.db import Database, NEW_COLUMNS, CfCookieStore


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
    "cloudflare_cookies",
}


def test_fresh_db_creates_all_12_tables(tmp_path):
    db = Database(tmp_path / "signals.db")
    names = {
        row["name"]
        for row in db.query("SELECT name FROM sqlite_master WHERE type='table'")
        if not row["name"].startswith("sqlite_")
    }
    assert EXPECTED_TABLES.issubset(names)
    assert len(EXPECTED_TABLES) == 12
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


def test_cf_cookie_roundtrip(tmp_path):
    db = Database(tmp_path / "s.db")
    store = CfCookieStore(db)
    assert store.get("acme.com", user_agent="Mozilla/1", proxy="direct") is None  # none yet

    store.put("acme.com", user_agent="Mozilla/1", proxy="direct",
              cookies=[{"name": "cf_clearance", "value": "tok123", "domain": "acme.com"},
                       {"name": "__cf_bm", "value": "bm1", "domain": ".acme.com"}],
              expires_at="2026-08-24T00:00:00+00:00")
    row = store.get("acme.com", user_agent="Mozilla/1", proxy="direct")
    assert row is not None
    assert row["user_agent"] == "Mozilla/1"
    cookies = json.loads(row["cookies"])
    assert cookies[0]["name"] == "cf_clearance"
    assert cookies[1]["name"] == "__cf_bm"


def test_cf_cookie_ua_mismatch_rejected(tmp_path):
    db = Database(tmp_path / "s.db")
    store = CfCookieStore(db)
    store.put("acme.com", user_agent="Mozilla/1", proxy="direct",
              cookies=[{"name": "cf_clearance", "value": "x"}],
              expires_at="2026-08-24T00:00:00+00:00")
    assert store.get("acme.com", user_agent="Mozilla/2", proxy="direct") is None  # UA-bound


def test_cf_cookie_proxy_mismatch_rejected(tmp_path):
    db = Database(tmp_path / "s.db")
    store = CfCookieStore(db)
    store.put("acme.com", user_agent="UA", proxy="http://proxy:8080",
              cookies=[{"name": "cf_clearance", "value": "x"}],
              expires_at="2026-08-24T00:00:00+00:00")
    assert store.get("acme.com", user_agent="UA", proxy="direct") is None  # proxy-bound


def test_cf_cookie_expired_rejected(tmp_path):
    db = Database(tmp_path / "s.db")
    store = CfCookieStore(db)
    store.put("acme.com", user_agent="UA", proxy="direct",
              cookies=[{"name": "cf_clearance", "value": "x"}],
              expires_at="2020-01-01T00:00:00+00:00")
    assert store.get("acme.com", user_agent="UA", proxy="direct") is None  # past expiry


def test_cf_cookie_fresh_solve_overwrites_stale(tmp_path):
    db = Database(tmp_path / "s.db")
    store = CfCookieStore(db)
    store.put("acme.com", user_agent="UA", proxy="direct",
              cookies=[{"name": "cf_clearance", "value": "old"}],
              expires_at="2026-08-24T00:00:00+00:00")
    # fresh solve must overwrite, not be silently dropped by COALESCE
    store.put("acme.com", user_agent="UA", proxy="direct",
              cookies=[{"name": "cf_clearance", "value": "new"}],
              expires_at="2026-08-25T00:00:00+00:00")
    row = store.get("acme.com", user_agent="UA", proxy="direct")
    cookies = json.loads(row["cookies"])
    assert cookies[0]["value"] == "new"


def test_cf_cookie_clear(tmp_path):
    db = Database(tmp_path / "s.db")
    store = CfCookieStore(db)
    store.put("acme.com", user_agent="UA", proxy="direct",
              cookies=[{"name": "cf_clearance", "value": "x"}],
              expires_at="2026-08-24T00:00:00+00:00")
    store.clear("acme.com")
    assert store.get("acme.com", user_agent="UA", proxy="direct") is None
