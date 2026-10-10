"""RRF: rank-based fusion, sign-convention-agnostic. MMR: diversity with coverage."""
from __future__ import annotations

import pytest

from src.retrieval.fusion import mmr_select, rrf_fuse


def test_rrf_sums_inverse_ranks():
    fused = rrf_fuse([["a", "b", "c"], ["b", "d"]], k=60)
    scores = dict(fused)
    assert scores["b"] > scores["a"]        # b ranks high on BOTH legs
    assert scores["b"] == pytest.approx(1/62 + 1/61)  # rank 2 (0-based 1) on leg 1, rank 1 on leg 2
    assert scores["d"] == pytest.approx(1/62)
    assert scores["c"] == pytest.approx(1/63)


def test_rrf_order_by_rank_only_not_score_sign():
    # bm25 gives lower-is-better, cosine higher-is-better — RRF never sees scores,
    # only pre-ordered id lists. A reversed second leg flips the fused order.
    up = rrf_fuse([["x", "y"], ["x", "y"]])
    assert up[0][0] == "x"


def test_rrf_empty_and_single_leg():
    assert rrf_fuse([]) == []
    assert rrf_fuse([["a", "b"]])[0][0] == "a"
    assert rrf_fuse([[], []]) == []


def test_rrf_deterministic_ties():
    a = rrf_fuse([["p", "q"], ["r", "s"]])
    b = rrf_fuse([["p", "q"], ["r", "s"]])
    assert a == b                            # stable ordering on equal scores


def test_mmr_prefers_diverse_set():
    import numpy as np
    # three candidates: near-identical x1/x2, and distinct z
    vecs = {"x1": np.array([1.0, 0.0]), "x2": np.array([0.99, 0.1]),
            "z": np.array([0.0, 1.0])}
    ranked = [("x1", 0.9), ("x2", 0.85), ("z", 0.8)]   # relevance order
    picked = mmr_select(ranked, vecs, lam=0.7, k=2)
    ids = [cid for cid, _ in picked]
    assert ids[0] == "x1"                    # most relevant first
    assert "z" in ids                        # diversity beats a near-dup
    assert "x2" not in ids


def test_mmr_keeps_unvectorised_items_never_drops():
    ranked = [("a", 0.9), ("b", 0.5)]
    picked = mmr_select(ranked, {}, lam=0.7, k=5)   # no vectors at all
    assert [cid for cid, _ in picked] == ["a", "b"]  # passthrough, order kept


def test_mmr_passthrough_when_k_at_or_above_pool():
    # pins the early-return contract the wiring relies on: k >= pool means
    # NO diversity pass — select_stage_docs must always call with k < pool
    # (the review caught the original wiring calling k=150 over a 50-pool:
    # MMR was a silent no-op and no test noticed).
    import numpy as np
    vecs = {"a": np.array([1.0, 0.0]), "b": np.array([0.0, 1.0])}
    ranked = [("a", 0.9), ("b", 0.5)]
    picked = mmr_select(ranked, vecs, lam=0.7, k=2)   # k == len(ranked)
    assert [cid for cid, _ in picked] == ["a", "b"]
