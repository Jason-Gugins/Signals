"""index command: delegates to index_domain, honors --domain/--limit, honest stats.

Offline lane: `src.retrieval.embedder.get_embedder` is monkeypatched to a
deterministic fake (cli.py imports it lazily inside the command body, so the
module-attribute patch lands). The store is built under tmp_path/data so the
CLI's default relative storage paths (data/signals.db, data/raw) resolve to
the same files after monkeypatch.chdir.
"""
from __future__ import annotations

import hashlib

import pytest
from click.testing import CliRunner

from src.cli import main
from src.core.db import Database
from src.core.rawstore import RawStore
from src.retrieval import indexer


@pytest.fixture(autouse=True)
def _clean_db_env(monkeypatch):
    """tests/test_cli_g2.py leaks SIGNALS_DB_PATH into the process env (set
    directly, never restored); Config.load honors it (config.py env-override
    pass), redirecting cfg.storage.db_path to a stale temp path while raw_dir
    still resolves against cwd. Clear it so the default relative storage
    paths — and therefore the CLI's Database — land in tmp_path."""
    monkeypatch.delenv("SIGNALS_DB_PATH", raising=False)


class FakeEmbedder:
    """Deterministic 16-dim 'embeddings': token-hash bag, L2-normalised.

    Copied (not imported) from tests/test_retrieval_indexer.py — test files
    never import across each other.
    """

    model = "fake@1"

    def embed(self, texts):
        import math
        out = []
        for t in texts:
            v = [0.0] * 16
            for w in t.lower().split():
                v[int(hashlib.sha1(w.encode()).hexdigest(), 16) % 16] += 1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out


def _store(tmp_path):
    """3 documents for acme.com (2 indexable HTML + 1 empty body)."""
    db = Database(tmp_path / "data" / "signals.db")
    rs = RawStore(db, tmp_path / "data" / "raw")
    for i, body in enumerate([
        b"<html><body><p>Acme is hiring a head of sales in Austin.</p></body></html>",
        b"<html><body><p>Quarterly SEC filing 8-K item 1.03 bankruptcy signal.</p></body></html>",
        b"",   # empty body: skipped_no_text, but COVERED in index_state
    ]):
        rs.put(source="test", url=f"https://acme.com/p{i}", body=body,
               content_type="text/html", status=200, domain="acme.com")
    return db, rs


def _count(db_path, table):
    return int(Database(db_path).one(f"SELECT COUNT(*) AS n FROM {table}")["n"])


def test_index_command_runs_offline(tmp_path, monkeypatch):
    db, rs = _store(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("src.retrieval.embedder.get_embedder", lambda: FakeEmbedder())
    runner = CliRunner()
    r = runner.invoke(main, ["index", "--domain", "acme.com"])
    assert r.exit_code == 0, r.output
    assert "domain=acme.com" in r.output
    assert "indexed=2" in r.output
    assert "skipped_no_text=1" in r.output
    # A fresh handle sees the CLI's writes: chunks + vectors + ledger rows.
    db_path = tmp_path / "data" / "signals.db"
    assert _count(db_path, "document_chunks") >= 2
    assert _count(db_path, "chunk_vectors") >= 2
    assert _count(db_path, "index_state") == 3


def test_index_stats_reports_counts_without_indexing(tmp_path, monkeypatch):
    db, rs = _store(tmp_path)
    indexer.index_domain(db, rs, "acme.com", embedder=FakeEmbedder())
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("src.retrieval.embedder.get_embedder", lambda: FakeEmbedder())
    db_path = tmp_path / "data" / "signals.db"
    before = {t: _count(db_path, t) for t in
              ("document_chunks", "chunk_vectors", "index_state", "chunks_fts")}
    runner = CliRunner()
    r = runner.invoke(main, ["index", "--stats", "--domain", "acme.com"])
    assert r.exit_code == 0, r.output
    assert "domain=acme.com" in r.output
    assert "docs=3" in r.output
    assert "covered=3" in r.output      # 2 indexed + 1 no_text BOTH count as covered
    assert "chunks=" in r.output
    assert "model=fake@1" in r.output
    # --stats writes NOTHING
    after = {t: _count(db_path, t) for t in
             ("document_chunks", "chunk_vectors", "index_state", "chunks_fts")}
    assert after == before


def test_rebuild_fts_restores_shadow(tmp_path, monkeypatch):
    db, rs = _store(tmp_path)
    indexer.index_domain(db, rs, "acme.com", embedder=FakeEmbedder())
    rows_before = _count(tmp_path / "data" / "signals.db", "chunks_fts")
    db.execute("DELETE FROM chunks_fts")
    assert db.query(
        "SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH '\"head of sales\"'"
    ) == []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("src.retrieval.embedder.get_embedder", lambda: FakeEmbedder())
    runner = CliRunner()
    r = runner.invoke(main, ["index", "--rebuild-fts"])
    assert r.exit_code == 0, r.output
    assert f"rows={rows_before}" in r.output
    # MATCH works again from the re-inserted shadow rows
    db2 = Database(tmp_path / "data" / "signals.db")
    hits = db2.query(
        "SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH '\"head of sales\"'"
    )
    assert hits, "rebuilt chunks_fts must find the hiring chunk again"


def test_index_dry_run_plans_only_writes_nothing(tmp_path, monkeypatch):
    db, rs = _store(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("src.retrieval.embedder.get_embedder", lambda: FakeEmbedder())
    runner = CliRunner()
    r = runner.invoke(main, ["--dry-run", "index", "--domain", "acme.com"])
    assert r.exit_code == 0, r.output
    assert "domain=acme.com" in r.output
    assert "would_index=3" in r.output
    db_path = tmp_path / "data" / "signals.db"
    assert _count(db_path, "document_chunks") == 0
    assert _count(db_path, "index_state") == 0


def test_dry_run_rebuild_fts_plans_only_writes_nothing(tmp_path, monkeypatch):
    """--dry-run index --rebuild-fts must WRITE NOTHING: the rebuild branch
    runs before the per-domain loop, so it needs its own dry-run guard — a
    global dry-run that still deletes + re-inserts chunks_fts violates the
    "plans only, writes nothing" contract (review finding)."""
    db, rs = _store(tmp_path)
    indexer.index_domain(db, rs, "acme.com", embedder=FakeEmbedder())
    db_path = tmp_path / "data" / "signals.db"
    rows_full = _count(db_path, "chunks_fts")
    assert rows_full >= 2
    # Break the shadow so a REAL rebuild would observably change the table.
    db.execute(
        "DELETE FROM chunks_fts WHERE chunk_id IN "
        "(SELECT chunk_id FROM document_chunks LIMIT 1)"
    )
    broken = _count(db_path, "chunks_fts")
    assert broken < rows_full

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("src.retrieval.embedder.get_embedder", lambda: FakeEmbedder())
    runner = CliRunner()
    r = runner.invoke(main, ["--dry-run", "index", "--rebuild-fts"])

    assert r.exit_code == 0, r.output
    assert "would rebuild chunks_fts" in r.output
    assert f"rows={rows_full}" in r.output  # would-rebuild count == full chunk count
    # Nothing written: the broken shadow is STILL broken.
    assert _count(db_path, "chunks_fts") == broken


def test_index_null_embedder_prints_remediation(tmp_path, monkeypatch):
    from src.retrieval.embedder import NullEmbedder

    db, rs = _store(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("src.retrieval.embedder.get_embedder", lambda: NullEmbedder())
    runner = CliRunner()
    r = runner.invoke(main, ["index", "--domain", "acme.com"])
    assert r.exit_code == 0, r.output
    assert 'pip install -e ".[rerank]"' in r.output
    db_path = tmp_path / "data" / "signals.db"
    assert _count(db_path, "document_chunks") == 0
