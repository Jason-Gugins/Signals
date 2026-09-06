"""Task 24: DB maintenance — migration v6 hot-path indexes + cookie expiry in prune_all."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.db import LATEST_VERSION, Database, prune_all  # noqa: E402


@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "signals.db")
    yield d
    d.close()


def _index_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    return {r[0] for r in rows}


class TestMigrationV6Indexes:
    def test_user_version_is_7(self, db):
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_VERSION == 7

    def test_fetchlog_source_at_index_exists(self, db):
        assert "idx_fetchlog_source_at" in _index_names(db.conn)
        # Columns actually (source, at) — not swapped.
        cols = [
            r[2]
            for r in db.conn.execute("PRAGMA index_info(idx_fetchlog_source_at)").fetchall()
        ]
        assert cols == ["source", "at"]

    def test_playassignments_domain_covered_by_pk(self, db):
        # The v6 review noted a named domain index would be dead weight: the
        # PK (domain, play_id, signal_id) autoindex already covers domain
        # lookups. Assert the PK route is what SQLite actually uses.
        plan = " ".join(
            str(r[3])
            for r in db.conn.execute(
                "EXPLAIN QUERY PLAN SELECT play_id FROM play_assignments WHERE domain=?",
                ("acme.com",),
            ).fetchall()
        )
        assert "PRIMARY KEY" in plan or "sqlite_autoindex" in plan
        assert "idx_playassignments_domain" not in _index_names(db.conn)

    def test_query_plans_use_the_indexes(self, db):
        plan = " ".join(
            str(r[3])
            for r in db.conn.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM fetch_log WHERE source=? AND at < ?",
                ("g2", "2026-01-01"),
            ).fetchall()
        )
        assert "idx_fetchlog_source_at" in plan
        plan2 = " ".join(
            str(r[3])
            for r in db.conn.execute(
                "EXPLAIN QUERY PLAN SELECT play_id FROM play_assignments WHERE domain=?",
                ("example.com",),
            ).fetchall()
        )
        # PK (domain, play_id) autoindex already covers domain=? lookups; just
        # verify the plan is a SEARCH, not a full SCAN.
        assert "SEARCH play_assignments" in plan2


class TestPruneCookieExpiry:
    def _seed_cookies(self, db):
        live = (
            "example.com",
            "ua-live",
            "direct",
            "[]",
            "2099-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
        )
        expired = (
            "old.com",
            "ua-old",
            "direct",
            "[]",
            "2020-01-01T00:00:00+00:00",
            "2019-01-01T00:00:00+00:00",
        )
        db.conn.execute(
            "INSERT INTO cloudflare_cookies(domain,user_agent,proxy,cookies,expires_at,solved_at)"
            " VALUES (?,?,?,?,?,?)",
            (live[0], live[1], live[2], live[3], live[4], live[5]),
        )
        db.conn.execute(
            "INSERT INTO cloudflare_cookies(domain,user_agent,proxy,cookies,expires_at,solved_at)"
            " VALUES (?,?,?,?,?,?)",
            (expired[0], expired[1], expired[2], expired[3], expired[4], expired[5]),
        )
        db.conn.execute(
            "INSERT INTO datadome_cookies(domain,user_agent,proxy,cookies,expires_at)"
            " VALUES (?,?,?,?,?)",
            (live[0], live[1], live[2], live[3], live[4]),
        )
        db.conn.execute(
            "INSERT INTO datadome_cookies(domain,user_agent,proxy,cookies,expires_at)"
            " VALUES (?,?,?,?,?)",
            (expired[0], expired[1], expired[2], expired[3], expired[4]),
        )
        db.conn.commit()

    def test_prune_deletes_only_expired_cookies(self, db):
        self._seed_cookies(db)
        counts = prune_all(db, keep_days=365)
        # Only the expired rows (one per table) are gone.
        cf = {r[0] for r in db.conn.execute("SELECT domain FROM cloudflare_cookies")}
        dd = {r[0] for r in db.conn.execute("SELECT domain FROM datadome_cookies")}
        assert cf == {"example.com"}
        assert dd == {"example.com"}
        assert counts["cookies"] == 2

    def test_counts_dict_gains_cookies_key(self, db):
        counts = prune_all(db, keep_days=30)
        assert "cookies" in counts
        assert counts["cookies"] == 0
        # Original keys still present.
        for key in ("raw_files", "documents", "fetch_log", "runs"):
            assert key in counts
