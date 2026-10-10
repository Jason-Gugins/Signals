"""Doctor retrieval_readiness check (intel hybrid-retrieval wave, plan T10).

The check probes the prerequisites of the opt-in `retrieval:` block:
onnxruntime (the [rerank] extra — the embedder/vector leg), a populated
`document_chunks` index, and FTS5 in the SQLite build (the BM25 leg). Each
missing prerequisite is a WARN naming the disabled leg and, for the empty
index, the remediation command. When retrieval is disabled (the shipped
default) the check reports OK and never warns — an off feature is not a
problem to report, and intel stages the recency tail exactly as before.
Pure import/:memory: probes: deterministic under --no-network (no model
downloads), so the tests stub importability instead of the network.
"""

from __future__ import annotations

import builtins
import sqlite3
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.config import Config
from src.core.db import Database
from src.pipeline.health import doctor


def _make_config(tmp_path, *, enabled: bool) -> Config:
    cfg = Config()
    cfg.contact_email = "ops@example.com"
    cfg.config_dir = str(tmp_path / "cfg")
    cfg.storage.raw_dir = str(tmp_path / "raw")
    cfg.storage.export_dir = str(tmp_path / "ex")
    cfg.storage.briefs_dir = str(tmp_path / "br")
    cfg.external_dbs.linkedin_db = str(tmp_path / "missing.db")
    cfg.external_dbs.repvue_db = str(tmp_path / "missing2.db")
    cfg.retrieval.enabled = enabled
    return cfg


def _row(rows):
    return [r for r in rows if r[0] == "retrieval_readiness"][0]


def _seed_chunks(db, n: int = 1) -> None:
    for i in range(n):
        db.execute(
            "INSERT INTO document_chunks(chunk_id, doc_id, domain, ordinal,"
            " char_start, char_end, text, token_est, model_id, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,datetime('now'))",
            (f"c{i}", f"d{i}", "acme.com", 0, 0, 10, "hiring head of sales", 5, "fake@1"),
        )


def _block_onnxruntime(monkeypatch):
    """Force the [rerank] extra absent regardless of the venv (import probe)."""
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "onnxruntime":
            raise ImportError("onnxruntime not installed")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def _stub_onnxruntime(monkeypatch):
    """Force the [rerank] extra present regardless of the venv (sys.modules hit)."""
    monkeypatch.setitem(sys.modules, "onnxruntime", types.ModuleType("onnxruntime"))


def test_disabled_reports_ok_even_without_extra(tmp_path, monkeypatch):
    # Off path is quiet: enabled=false must be OK even with the extra missing
    # and the index empty — do not warn on the off path (shipped default).
    _block_onnxruntime(monkeypatch)
    db = Database(tmp_path / "s.db")
    rows = doctor(_make_config(tmp_path, enabled=False), db, check_network=False)
    name, status, detail = _row(rows)
    assert name == "retrieval_readiness"
    assert status == "OK"
    assert "disabled" in detail


def test_enabled_ok_when_extra_and_index_present(tmp_path, monkeypatch):
    _stub_onnxruntime(monkeypatch)
    db = Database(tmp_path / "s.db")
    _seed_chunks(db, n=3)
    rows = doctor(_make_config(tmp_path, enabled=True), db, check_network=False)
    _, status, detail = _row(rows)
    assert status == "OK"
    assert "3" in detail  # names the indexed chunk count


def test_enabled_warns_when_extra_missing(tmp_path, monkeypatch):
    _block_onnxruntime(monkeypatch)
    db = Database(tmp_path / "s.db")
    _seed_chunks(db)  # index non-empty: the extra is the ONLY problem
    rows = doctor(_make_config(tmp_path, enabled=True), db, check_network=False)
    _, status, detail = _row(rows)
    assert status == "WARN"
    assert "retrieval enabled but the [rerank] extra is not installed" in detail
    assert "vector leg disabled" in detail
    assert "index is empty" not in detail  # single-problem WARN is honest


def test_enabled_warns_when_index_empty(tmp_path, monkeypatch):
    _stub_onnxruntime(monkeypatch)
    db = Database(tmp_path / "s.db")  # migrated, zero document_chunks rows
    rows = doctor(_make_config(tmp_path, enabled=True), db, check_network=False)
    _, status, detail = _row(rows)
    assert status == "WARN"
    assert "retrieval enabled but the index is empty; run: python -m src.cli index" in detail


def test_warns_when_fts5_unavailable(tmp_path, monkeypatch):
    _stub_onnxruntime(monkeypatch)
    db = Database(tmp_path / "s.db")
    _seed_chunks(db)

    class _NoFts5Conn:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("no such module: fts5")

        def close(self):
            pass

    # The probe runs on a temp :memory: connection, never the live db.
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: _NoFts5Conn())
    rows = doctor(_make_config(tmp_path, enabled=True), db, check_network=False)
    _, status, detail = _row(rows)
    assert status == "WARN"
    assert "SQLite build lacks FTS5; BM25 leg unavailable" in detail
