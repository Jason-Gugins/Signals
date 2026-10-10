"""Rank fusion and diversity selection. Pure functions: no I/O, no clock.

RRF (Cormack et al. 2009): score(chunk) = sum over legs of 1/(k + rank).
Rank-based, so no score normalisation and no per-query weight tuning — and
the two legs' incompatible score conventions (FTS5 bm25 is lower-is-better,
cosine is higher-is-better) never meet: callers pass pre-ordered id lists.
k=60 is the literature default.
"""
from __future__ import annotations

DEFAULT_RRF_K = 60


def rrf_fuse(
    rankings: list[list[str]], *, k: int = DEFAULT_RRF_K
) -> list[tuple[str, float]]:
    """Fuse ordered id lists -> [(id, rrf_score)] best-first. Deterministic."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, chunk_id in enumerate(ranking):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


def mmr_select(
    ranked: list[tuple[str, float]],
    vecs: dict[str, "object"],           # id -> L2-normalised vector (numpy row)
    *,
    lam: float = 0.7,
    k: int = 10,
) -> list[tuple[str, float]]:
    """Maximal Marginal Relevance over the fused shortlist.

    Vectors are L2-normalised upstream (dot product == cosine similarity), so
    the diversity term is a plain dot. Items without a vector are NEVER
    dropped: they trail the selected set in relevance order (an unvectorised
    item must not lose to a duplicate).
    """
    if len(ranked) <= k or not vecs:
        return list(ranked)
    import numpy as np

    selected: list[str] = []
    pool = [cid for cid, _ in ranked]
    rel = dict(ranked)
    out: list[tuple[str, float]] = []
    while pool and len(out) < k:
        best, best_score = None, None
        for cid in pool:
            v = vecs.get(cid)
            if v is None:
                div_penalty = 0.0
            elif selected:
                sims = max(float(np.dot(v, vecs[s])) for s in selected if s in vecs) \
                    if any(s in vecs for s in selected) else 0.0
                div_penalty = sims
            else:
                div_penalty = 0.0
            score = lam * rel[cid] - (1 - lam) * div_penalty
            if best_score is None or score > best_score:
                best, best_score = cid, score
        out.append((best, rel[best]))
        selected.append(best)
        pool.remove(best)
    # Vectorised leftovers lost the diversity competition for the k slots and
    # are DROPPED (a near-duplicate must not trail back in); items WITHOUT a
    # vector are never dropped — they trail the selected set in relevance
    # order (an unvectorised item must not lose to a duplicate).
    out.extend((cid, rel[cid]) for cid in pool if vecs.get(cid) is None)
    return out
