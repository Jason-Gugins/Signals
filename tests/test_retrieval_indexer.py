"""Indexer: idempotent, FTS-searchable, per-doc re-index on model change."""
from __future__ import annotations

import hashlib

import pytest

from src.core.db import Database
from src.core.models import Document
from src.core.rawstore import RawStore
from src.retrieval import indexer


class FakeEmbedder:
    """Deterministic 16-dim 'embeddings': token-hash bag, L2-normalised."""

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


@pytest.fixture()
def store(tmp_path):
    db = Database(tmp_path / "signals.db")
    rs = RawStore(db, tmp_path / "raw")
    for i, body in enumerate([
        b"<html><body><p>Acme is hiring a head of sales in Austin.</p></body></html>",
        b"<html><body><p>Quarterly SEC filing 8-K item 1.03 bankruptcy signal.</p></body></html>",
        b"",   # empty body: must be skipped, counted, never indexed
    ]):
        rs.put(source="test", url=f"https://acme.com/p{i}", body=body,
               content_type="text/html", status=200, domain="acme.com")
    return db, rs


def test_index_domain_creates_searchable_chunks(store):
    db, rs = store
    stats = indexer.index_domain(db, rs, "acme.com", embedder=FakeEmbedder())
    assert stats["docs_indexed"] == 2
    assert stats["skipped_no_text"] == 1
    assert stats["chunks"] >= 2
    rows = db.query("SELECT * FROM document_chunks WHERE domain='acme.com'")
    assert rows and all(r["model_id"] == "fake@1" for r in rows)
    hits = db.query(
        "SELECT c.chunk_id FROM chunks_fts f JOIN document_chunks c "
        "ON c.chunk_id = f.chunk_id WHERE chunks_fts MATCH '\"head of sales\"'"
    )
    assert hits, "FTS shadow must find the hiring chunk"
    vecs = db.query("SELECT chunk_id FROM chunk_vectors")
    assert {v["chunk_id"] for v in vecs} == {r["chunk_id"] for r in rows}
    states = {r["state"] for r in db.query(
        "SELECT state FROM index_state WHERE domain='acme.com'")}
    assert states == {"indexed", "no_text"}  # the empty-body doc is COVERED as no_text


def test_reindex_is_idempotent(store):
    db, rs = store
    s1 = indexer.index_domain(db, rs, "acme.com", embedder=FakeEmbedder())
    s2 = indexer.index_domain(db, rs, "acme.com", embedder=FakeEmbedder())
    assert s2["docs_indexed"] == 0
    assert s2["docs_seen"] == s1["docs_indexed"] + s2["skipped_no_text"]


def test_model_change_reindexes(store):
    db, rs = store

    class FakeEmbedderV2(FakeEmbedder):
        model = "fake@2"

    indexer.index_domain(db, rs, "acme.com", embedder=FakeEmbedder())
    s = indexer.index_domain(db, rs, "acme.com", embedder=FakeEmbedderV2())
    assert s["docs_indexed"] == 2
    rows = db.query("SELECT DISTINCT model_id FROM document_chunks WHERE domain='acme.com'")
    assert [r["model_id"] for r in rows] == ["fake@2"]


def test_html_extracted_before_chunking(store):
    db, rs = store
    indexer.index_domain(db, rs, "acme.com", embedder=FakeEmbedder())
    texts = [r["text"] for r in db.query("SELECT text FROM document_chunks")]
    assert all("<" not in t.split()[0] for t in texts if t.split())
    assert any("head of sales" in t for t in texts)
