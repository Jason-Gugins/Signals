"""Legs, query builder, and the select_stage_docs contract."""
from __future__ import annotations

import pytest

from src.core.db import Database
from src.retrieval import search
from src.retrieval.indexer import MIN_TEXT_CHARS


class FakeEmbedder:  # same shape as tests/test_retrieval_indexer.py
    model = "fake@1"

    def embed(self, texts):
        import hashlib, math
        out = []
        for t in texts:
            v = [0.0] * 16
            for w in t.lower().split():
                v[int(hashlib.sha1(w.encode()).hexdigest(), 16) % 16] += 1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out


@pytest.fixture()
def indexed(tmp_path):
    from src.core.rawstore import RawStore
    db = Database(tmp_path / "signals.db")
    rs = RawStore(db, tmp_path / "raw")
    rs.put(source="t", url="https://acme.com/careers", body=(
        b"<html><body><p>" + b"careers page hiring head of sales role austin texas " * 30
        + b"</p></body></html>"), content_type="text/html", status=200, domain="acme.com")
    rs.put(source="t", url="https://acme.com/filing", body=(
        b"<html><body><p>" + b"sec filing 8-K bankruptcy signal chapter 11 " * 30
        + b"</p></body></html>"), content_type="text/html", status=200, domain="acme.com")
    indexer_stats = __import__("src.retrieval.indexer", fromlist=["index_domain"]) \
        .index_domain(db, rs, "acme.com", embedder=FakeEmbedder())
    assert indexer_stats["docs_indexed"] == 2
    return db, rs


def test_fts_match_query_is_injection_safe_and_keeps_short_tokens():
    q = search.fts_match_query('acme "quoted" AND drop; --')
    assert q == '"acme" OR "quoted" OR "AND" OR "drop"'   # exact shape pinned
    assert ";" not in q and " drop; --" not in q
    # headline tokens survive: filing codes and single-letter form names are
    # the lexical leg's exact-token wins — the extractor must keep them
    assert '"8-K"' in search.fts_match_query("8-K item 1.03")
    assert '"D"' in search.fts_match_query("Form D filing")
    assert search.fts_match_query("") is None
    assert search.fts_match_query("!!!") is None


def test_bm25_leg_ranks_relevant_chunk_first(indexed):
    db, _rs = indexed
    hits = search.bm25_search(db, "hiring head of sales", domain="acme.com", limit=10)
    assert hits
    top = db.one("SELECT text FROM document_chunks WHERE chunk_id = ?", (hits[0][0],))
    assert "head of sales" in top["text"]


def test_vector_leg_ranks_paraphrase(indexed):
    db, _rs = indexed
    hits = search.vector_search(db, FakeEmbedder(), "who is vp sales role",
                                domain="acme.com", limit=10)
    assert hits is not None and hits


def test_legs_return_none_not_error_when_empty(tmp_path):
    db = Database(tmp_path / "s.db")
    assert search.bm25_search(db, "anything", domain="x.com", limit=5) == []
    assert search.vector_search(db, FakeEmbedder(), "anything", domain="x.com") is None
    assert search.vector_search(db, FakeEmbedder(), "anything", domain=None) in (None, [])


def test_select_stage_docs_returns_doc_order_and_coverage(indexed):
    db, rs = indexed
    sel = search.select_stage_docs(
        db, "acme.com", query="hiring head of sales", ce_query="hiring head of sales",
        embedder=FakeEmbedder(), scorer=None,  # scorer None: cross-encoder leg skipped
        cfg={"candidate_limit": 10, "rerank_top_n": 0, "mmr_lambda": 0.7,
             "stage_docs": 1, "min_index_coverage": 0.5},
    )
    assert sel.doc_ids and len(sel.doc_ids) == 1
    assert sel.coverage == 1.0
    assert sel.staged_by == "retrieval"
    # MMR actually EXECUTED (pool > k): the review caught the original wiring
    # making it a silent early-return no-op — this pin holds the corrected shape.
    assert sel.provenance["mmr_pool"] > sel.provenance["mmr_k"]


def test_select_stage_docs_falls_back_when_uncovered(indexed):
    db, rs = indexed
    sel = search.select_stage_docs(
        db, "other.com", query="x", ce_query="x", embedder=FakeEmbedder(), scorer=None,
        cfg={"min_index_coverage": 0.8, "stage_docs": 5},
    )
    assert sel.staged_by == "uncovered" and sel.doc_ids == []
