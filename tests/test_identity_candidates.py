"""Plan T2 — identity_candidates review-queue tests.

Covers:
- migration v7 (table created on a fresh db + user_version bump)
- migration on the v6 -> v7 upgrade path
- IdentityCandidateStore: idempotent upsert on (name, kind) that replaces
  candidates and resets the row to pending
- pending_candidates: kind filter + candidates_json parsing
- set_decision validation: rejected needs no domain, accepted requires one,
  invalid status raises
- doctor check identity_candidates_pending: OK on an empty queue, WARN
  with pending rows
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.config import Config
from src.core.db import Database, IdentityCandidateStore
from src.pipeline.health import doctor


def _store(tmp_path: Path) -> tuple[Database, IdentityCandidateStore]:
    db = Database(tmp_path / "cand.db")
    return db, IdentityCandidateStore(db)


def _make_config(tmp_path: Path) -> Config:
    cfg = Config()
    cfg.contact_email = "ops@example.com"
    cfg.config_dir = str(tmp_path / "cfg")
    cfg.storage.raw_dir = str(tmp_path / "raw")
    cfg.storage.export_dir = str(tmp_path / "ex")
    cfg.storage.briefs_dir = str(tmp_path / "br")
    cfg.external_dbs.linkedin_db = str(tmp_path / "missing.db")
    cfg.external_dbs.repvue_db = str(tmp_path / "missing2.db")
    return cfg


# ── migration v7: identity_candidates table ────────────────────────────────

def test_migration_v7_creates_identity_candidates_and_bumps_version(tmp_path: Path) -> None:
    db = Database(tmp_path / "fresh.db")
    try:
        version = db.one("PRAGMA user_version")["user_version"]
        assert version >= 7
        cols = db.table_columns("identity_candidates")
        for col in ("name", "kind", "candidates_json", "chosen_domain", "status", "source", "created_at"):
            assert col in cols
        # PK (name, kind): a duplicate pair must fail.
        db.execute(
            "INSERT INTO identity_candidates (name, kind, candidates_json, created_at) "
            "VALUES ('acme', 'account', '[]', '2026-01-01T00:00:00+00:00')"
        )
        with pytest.raises(Exception):
            db.execute(
                "INSERT INTO identity_candidates (name, kind, candidates_json, created_at) "
                "VALUES ('acme', 'account', '[]', '2026-01-01T00:00:00+00:00')"
            )
        # status defaults to 'pending'.
        row = db.one("SELECT status FROM identity_candidates WHERE name = 'acme'")
        assert row["status"] == "pending"
    finally:
        db.close()


def test_migration_v7_upgrades_old_db(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    db = Database(path)
    db.execute("DROP TABLE identity_candidates")
    db.execute("PRAGMA user_version = 6")
    db.close()

    db2 = Database(path)
    try:
        assert "name" in db2.table_columns("identity_candidates")
        assert db2.one("PRAGMA user_version")["user_version"] >= 7
    finally:
        db2.close()


# ── IdentityCandidateStore: upsert / pending / decision ───────────────────

def test_upsert_is_idempotent_and_replaces_candidates(tmp_path: Path) -> None:
    db, store = _store(tmp_path)
    try:
        store.upsert_candidate(
            "Acme Inc", "account",
            [{"domain": "acme.com", "rank": 1, "evidence": "wikidata P856"}],
            source="wikidata",
        )
        # Human accepts one candidate...
        store.set_decision("Acme Inc", "account", "accepted", chosen_domain="acme.com")
        # ...then a re-discovery replaces the whole row (same raw name —
        # the store is deliberately thin: normalization is the resolvers' job).
        store.upsert_candidate(
            "Acme Inc", "account",
            [{"domain": "acme.org", "rank": 1, "evidence": "gkg"}],
            source="gkg",
        )
        rows = db.query("SELECT * FROM identity_candidates")
        assert len(rows) == 1  # idempotent on (name, kind)
        row = rows[0]
        assert row["status"] == "pending"  # reset on re-upsert
        assert row["chosen_domain"] is None  # cleared on re-upsert
        assert row["source"] == "gkg"
        assert row["created_at"]  # timestamp injected by the store
    finally:
        db.close()


def test_pending_candidates_filters_by_kind_and_parses_json(tmp_path: Path) -> None:
    db, store = _store(tmp_path)
    try:
        candidates = [
            {"domain": "globex.com", "rank": 1},
            {"domain": "globex.net", "rank": 2},
        ]
        store.upsert_candidate("Globex", "account", candidates, source="wikidata")
        store.upsert_candidate("Initech", "competitor", [{"domain": "initech.com"}])
        # A decided row must drop out of the pending queue.
        store.set_decision("Initech", "competitor", "rejected")

        pending = store.pending_candidates()
        assert [r["name"] for r in pending] == ["Globex"]
        assert pending[0]["candidates"] == candidates  # parsed from candidates_json

        # kind filter: nothing pending for 'competitor' anymore.
        assert store.pending_candidates(kind="competitor") == []
        store.upsert_candidate("Umbrella", "competitor", [{"domain": "umbrella.com"}])
        comp = store.pending_candidates(kind="competitor")
        assert [r["name"] for r in comp] == ["Umbrella"]
        assert comp[0]["candidates"] == [{"domain": "umbrella.com"}]
    finally:
        db.close()


def test_set_decision_rejected_needs_no_domain(tmp_path: Path) -> None:
    db, store = _store(tmp_path)
    try:
        store.upsert_candidate("Soylent", "account", [{"domain": "soylent.com"}])
        store.set_decision("Soylent", "account", "rejected")
        row = db.one("SELECT * FROM identity_candidates WHERE name = 'Soylent'")
        assert row["status"] == "rejected"
        assert store.pending_candidates() == []
    finally:
        db.close()


def test_set_decision_accepted_requires_chosen_domain(tmp_path: Path) -> None:
    db, store = _store(tmp_path)
    try:
        store.upsert_candidate("Hooli", "account", [{"domain": "hooli.com"}])
        with pytest.raises(ValueError):
            store.set_decision("Hooli", "account", "accepted")
        with pytest.raises(ValueError):
            store.set_decision("Hooli", "account", "accepted", chosen_domain="")
        # A valid accept records the domain and leaves the queue.
        store.set_decision("Hooli", "account", "accepted", chosen_domain="hooli.com")
        row = db.one("SELECT * FROM identity_candidates WHERE name = 'Hooli'")
        assert row["status"] == "accepted"
        assert row["chosen_domain"] == "hooli.com"
        assert store.pending_candidates() == []
    finally:
        db.close()


def test_set_decision_rejects_invalid_status(tmp_path: Path) -> None:
    db, store = _store(tmp_path)
    try:
        store.upsert_candidate("Vandelay", "account", [{"domain": "vandelay.com"}])
        with pytest.raises(ValueError):
            store.set_decision("Vandelay", "account", "maybe")
        with pytest.raises(ValueError):
            store.set_decision("Vandelay", "account", "pending")
        # The row is untouched by the failed decisions.
        assert db.one("SELECT status FROM identity_candidates WHERE name = 'Vandelay'")["status"] == "pending"
    finally:
        db.close()


# ── doctor check: identity_candidates_pending ──────────────────────────────

def _doctor_check(cfg: Config, db: Database) -> tuple[str, str]:
    rows = doctor(cfg, db, check_network=False)
    checks = {name: (status, detail) for name, status, detail in rows}
    assert "identity_candidates_pending" in checks
    return checks["identity_candidates_pending"]


def test_doctor_pending_ok_on_empty_queue(tmp_path: Path) -> None:
    db, _ = _store(tmp_path)
    try:
        status, detail = _doctor_check(_make_config(tmp_path), db)
        assert status == "OK"
        assert "no pending identity candidates" in detail
    finally:
        db.close()


def test_doctor_pending_warns_with_pending_rows(tmp_path: Path) -> None:
    db, store = _store(tmp_path)
    try:
        store.upsert_candidate("Acme", "account", [{"domain": "acme.com"}])
        store.upsert_candidate("Initech", "account", [{"domain": "initech.com"}])
        status, detail = _doctor_check(_make_config(tmp_path), db)
        assert status == "WARN"
        assert detail == (
            "2 pending identity candidates "
            "(review via sweep --discover / candidates list)"
        )
    finally:
        db.close()
