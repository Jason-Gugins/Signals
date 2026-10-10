"""RawStore.prune must not orphan retrieval rows (review finding).

Deleting a documents row without its document_chunks / chunks_fts /
chunk_vectors / index_state rows leaves orphans behind — and orphaned
index_state rows keep counting toward retrieval coverage, which can mask
unindexed newest docs: coverage reads at/above the floor with no gap while
retrieval silently stages only stale docs.
"""
from __future__ import annotations

import hashlib

from src.core.db import Database
from src.core.rawstore import RawStore
from src.retrieval import indexer
from src.retrieval.search import select_stage_docs


class FakeEmbedder:  # same shape as the other retrieval test files
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


def _put(rs, i, body):
    return rs.put(source="t", url=f"https://acme.com/p{i}", body=body,
                  content_type="text/html", status=200, domain="acme.com")


def _n(db, table):
    return int(db.one(f"SELECT COUNT(*) AS n FROM {table}")["n"])


def test_prune_removes_all_retrieval_rows(tmp_path):
    """keep_days 0 prunes everything — none of the four retrieval tables may
    keep a row for a deleted document."""
    db = Database(tmp_path / "signals.db")
    rs = RawStore(db, tmp_path / "raw")
    _put(rs, 0, b"<html><body><p>Acme is hiring a head of sales in Austin.</p></body></html>")
    _put(rs, 1, b"<html><body><p>Quarterly SEC filing 8-K item 1.03 bankruptcy signal.</p></body></html>")
    st = indexer.index_domain(db, rs, "acme.com", embedder=FakeEmbedder())
    assert st["docs_indexed"] == 2
    for t in ("document_chunks", "chunks_fts", "chunk_vectors", "index_state"):
        assert _n(db, t) > 0, t

    n = rs.prune(0)

    assert n == 2
    for t in ("documents", "document_chunks", "chunks_fts", "chunk_vectors",
              "index_state"):
        assert _n(db, t) == 0, t


def test_prune_keeps_coverage_math_honest(tmp_path):
    """The coverage regression: index 2 docs, prune 1, add 1 new doc —
    select_stage_docs must report coverage 0.5 (1 covered / 2 total), not
    1.0 (the pruned doc's orphaned index_state row still counting)."""
    db = Database(tmp_path / "signals.db")
    rs = RawStore(db, tmp_path / "raw")
    d1 = _put(rs, 0, b"<html><body><p>Acme is hiring a head of sales in Austin.</p></body></html>")
    _put(rs, 1, b"<html><body><p>Quarterly SEC filing 8-K item 1.03 bankruptcy signal.</p></body></html>")
    # Backdate d1 so keep_days=1 prunes exactly it (d2 stays, d3 comes later).
    db.execute(
        "UPDATE documents SET fetched_at = ? WHERE doc_id = ?",
        ("2026-01-01T00:00:00+00:00", d1.doc_id),
    )
    st = indexer.index_domain(db, rs, "acme.com", embedder=FakeEmbedder())
    assert st["docs_indexed"] == 2

    assert rs.prune(1) == 1

    _put(rs, 2, b"<html><body><p>Brand new press release about the Acme launch.</p></body></html>")

    sel = select_stage_docs(
        db, "acme.com", query="hiring sales", ce_query="hiring sales",
        embedder=FakeEmbedder(), scorer=None,
        cfg={"min_index_coverage": 0.8},
    )
    assert sel.coverage == 0.5          # 1 covered (d2) / 2 total (d2, d3)
    assert sel.staged_by == "uncovered"  # below the floor: honest gap, not a mask
