"""Hybrid retrieval over the chunk index: BM25 + vectors -> RRF -> CE rerank -> MMR.

select_stage_docs() is the ONLY function intel calls. Everything returns
honest None/[] on unavailability; selection problems degrade to the caller's
recency path, never to a run failure.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# 1-char and hyphenated terms SURVIVE ("D" in "Form D", "8-K"): FTS5 composes
# a quoted phrase over the unicode61 token stream, so '"8-K"' matches the
# adjacent tokens "8","K" the tokenizer produced. Leading/trailing hyphens
# are excluded ("--" never becomes a term). The original {2,32} draft DROPPED
# 8-K and Form D — the very tokens the lexical leg exists for (review finding).
_WORD = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_-]{0,30}[A-Za-z0-9])?")
_MAX_QUERY_TERMS = 24


def fts_match_query(query: str) -> str | None:
    """Free text -> safe FTS5 MATCH string ('"t1" OR "t2"'). None when empty.

    Every term is double-quoted (FTS5 phrase syntax) with embedded quotes
    doubled — user text is never interpreted as FTS5 query syntax.
    """
    terms = [m.group(0) for m in _WORD.finditer(query)][:_MAX_QUERY_TERMS]
    if not terms:
        return None
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)


def bm25_search(db, query: str, *, domain: str | None, limit: int = 60):
    """FTS5 leg -> [(chunk_id, bm25_score), ...] best-first, or [] when unavailable.

    FTS5's bm25() is LOWER-is-better — ORDER BY ASC. RRF consumes ranks, so
    the convention never leaks. The score rides along for diagnostics only;
    select_stage_docs strips it before fusing.

    [] means "queried, nothing (lexically) matched" — deliberately distinct
    from the vector leg's None ("leg unavailable"), so call sites can tell
    an empty corpus from a missed query.
    """
    match = fts_match_query(query)
    if not match:
        return []
    sql = (
        "SELECT f.chunk_id AS chunk_id, bm25(chunks_fts) AS score "
        "FROM chunks_fts f JOIN document_chunks c ON c.chunk_id = f.chunk_id "
        "WHERE chunks_fts MATCH ?"
    )
    params: list = [match]
    if domain:
        sql += " AND c.domain = ?"
        params.append(domain)
    sql += " ORDER BY score ASC LIMIT ?"
    params.append(int(limit))
    try:
        rows = db.query(sql, params) or []
    except Exception:
        return []
    return [(r["chunk_id"], r["score"]) for r in rows]


def vector_search(db, embedder, query: str, *, domain: str | None, limit: int = 60):
    """Cosine leg -> [chunk_id] best-first; None when the leg is unavailable.

    Vectors are stored L2-normalised (dot == cosine). The BLOB set is
    filtered by `embedder.model` — the ONE identity accessor the indexer,
    the coverage ledger and this filter all share; a `.model`-vs-`model_id`
    mismatch here would silently query `model_id = NULL` and return nothing
    (review finding). None (not []) also covers "no vectors stored for this
    model/domain": nothing to search means the leg is unavailable, not that
    the query missed.
    """
    if embedder is None:
        return None
    qv = embedder.embed([query])
    if not qv:
        return None
    import numpy as np  # lazy: module stays import-safe without the extra

    sql = (
        "SELECT v.chunk_id AS chunk_id, v.vec AS vec "
        "FROM chunk_vectors v JOIN document_chunks c ON c.chunk_id = v.chunk_id "
        "WHERE v.model_id = ?"
    )
    params: list = [getattr(embedder, "model", None)]
    if domain:
        sql += " AND c.domain = ?"
        params.append(domain)
    rows = db.query(sql, params) or []
    if not rows:
        return None
    dim = len(qv[0])
    mat = np.frombuffer(b"".join(r["vec"] for r in rows), dtype="<f4").reshape(len(rows), dim)
    q = np.asarray(qv[0], dtype="<f4")
    sims = mat @ q
    order = sims.argsort()[::-1][: int(limit)]
    return [rows[i]["chunk_id"] for i in order]


def build_query(account, profile) -> tuple[str, str]:
    """(retrieval_query, ce_query) from account identity + market-profile vocabulary.

    The profile the operator already writes for `needs` steers retrieval for
    free; an empty profile degrades to identity terms. The CE query stays
    SHORT (<= 240 chars): the cross-encoder truncates the (query, chunk)
    pair at 256 tokens and a long query starves the chunk half.
    """
    name = str(getattr(account, "name", "") or "")
    domain = str(getattr(account, "domain", "") or "")
    ident = " ".join(t for t in (name, domain.split(".")[0] if domain else "") if t)
    phrases: list[str] = []
    if profile is not None:
        for off in tuple(getattr(profile, "offerings", ()) or ())[:5]:
            phrases += list(off.served_problem_phrases)[:6]
            phrases += list(off.buyer_departments)[:3]
            phrases += list(off.required_vendors)[:4]
            phrases += list(off.competitor_vendors)[:3]
    query = " ".join([ident] + phrases).strip()
    ce_query = " ".join([ident] + phrases[:8]).strip() or ident
    return query[:2000], ce_query[:240]


@dataclass
class StageSelection:
    doc_ids: list[str] = field(default_factory=list)
    coverage: float = 0.0
    staged_by: str = "uncovered"        # retrieval | uncovered | unavailable
    query: str = ""
    ce_query: str = ""
    provenance: dict = field(default_factory=dict)


def select_stage_docs(
    db,
    domain: str,
    *,
    query: str,
    ce_query: str,
    embedder,
    scorer,                              # rerank.get_scorer() result; None skips CE leg
    cfg: dict,
) -> StageSelection:
    """Rank the domain's chunk corpus and collapse it to an ordered doc-id list.

    Coverage = indexed docs / candidate docs for the domain; below
    min_index_coverage the caller falls back to recency with a gap line.
    """
    sel = StageSelection(query=query, ce_query=ce_query)
    cand = int(cfg.get("candidate_limit", 60))
    stage_docs = int(cfg.get("stage_docs", 35))
    min_cov = float(cfg.get("min_index_coverage", 0.8))
    model = getattr(embedder, "model", None)

    # Coverage from the index_state LEDGER, per active model: a doc the
    # indexer examined-and-could-not-index ('no_text') counts as COVERED —
    # leaving empty/binary bodies in the denominator as permanent gaps would
    # pin coverage below the floor forever and silently force recency
    # (review finding).
    total_docs = db.one("SELECT COUNT(*) AS n FROM documents WHERE domain = ?", (domain,))
    covered = db.one(
        "SELECT COUNT(DISTINCT doc_id) AS n FROM index_state WHERE domain = ? AND model_id = ?",
        (domain, model),
    )
    total = int(total_docs["n"]) if total_docs else 0
    have = int(covered["n"]) if covered else 0
    sel.coverage = (have / total) if total else 0.0
    if total == 0 or model is None or sel.coverage < min_cov:
        sel.staged_by = "uncovered"
        return sel

    legs: list[list[str]] = []
    bm = bm25_search(db, query, domain=domain, limit=cand)
    if bm:
        # bm25 tuples carry the raw score for diagnostics; RRF is rank-based
        # and consumes bare ids only.
        legs.append([cid for cid, _score in bm])
    vs = vector_search(db, embedder, query, domain=domain, limit=cand)
    if vs:
        legs.append(vs)
    if not legs:
        sel.staged_by = "unavailable"
        return sel
    from src.retrieval.fusion import mmr_select, rrf_fuse

    fused = rrf_fuse(legs, k=int(cfg.get("rrf_k", 60)))
    shortlist = fused[:cand]
    # Cross-encoder leg: scores ONLY the top rerank_top_n (it is the expensive
    # leg — scoring the whole candidate pool wastes it). Doc side ONLY:
    # score_pairs builds "f'{title} — {summary}'" (src/signals/rerank.py:85),
    # and the pair's query slot already carries the query — duplicating it on
    # the doc side burns ~60 of the 256-token budget (review finding). A
    # None return = model unavailable = skip the CE leg (keep RRF order).
    ce_scored = False
    if scorer is not None and ce_query:
        top = [cid for cid, _s in shortlist[: int(cfg.get("rerank_top_n", 24))]]
        texts = _chunk_texts(db, top)
        try:
            scores = scorer.score_pairs(ce_query, [(texts.get(cid, ""), "") for cid in top])
        except Exception:
            scores = None
        if scores:
            ce_scored = True
            floor = float(cfg.get("rerank_floor", 0.0))
            scored = {cid: s for cid, s in zip(top, scores) if s is not None and s >= floor}
            shortlist = (
                sorted(scored.items(), key=lambda kv: -kv[1])
                + [(cid, s) for cid, s in shortlist if cid not in scored]
            )
    # MMR over min-max-NORMALISED scores, k = stage_docs. RRF scores live in
    # a narrow band (~1/(k+rank): 0.008-0.033) while cosine lives in 0..1 —
    # unnormalised, lam*rel is ~5-10x smaller than (1-lam)*sim and "diversity"
    # silently dominates selection (review finding). And k must sit BELOW the
    # pool (candidate_limit > stage_docs by config contract): a k above the
    # pool makes mmr_select's early return a silent no-op (review finding —
    # the original k=stage_docs*3 never executed).
    lo = min(s for _c, s in shortlist)
    hi = max(s for _c, s in shortlist)
    span = (hi - lo) or 1.0
    norm = [(cid, (s - lo) / span) for cid, s in shortlist]
    vecs = _chunk_vectors(db, [cid for cid, _s in norm], model)
    picked = mmr_select(norm, vecs, lam=float(cfg.get("mmr_lambda", 0.7)), k=stage_docs)
    sel.provenance = {
        "chunks_considered": len(fused),
        "legs": len(legs),
        "ce_scored": int(ce_scored),
        "mmr_pool": len(norm),
        "mmr_k": stage_docs,
    }
    # collapse chunks -> docs (best chunk wins), keep order
    doc_by_chunk = _doc_map(db, [cid for cid, _s in picked])
    doc_order: list[str] = []
    seen: set[str] = set()
    for cid, _score in picked:
        did = doc_by_chunk.get(cid)
        if not did or did in seen:
            continue
        seen.add(did)
        doc_order.append(did)
        if len(doc_order) >= stage_docs:
            break
    sel.doc_ids = doc_order
    sel.staged_by = "retrieval"
    return sel


def _doc_map(db, chunk_ids: list[str]) -> dict[str, str]:
    if not chunk_ids:
        return {}
    rows = db.query(
        f"SELECT chunk_id, doc_id FROM document_chunks WHERE chunk_id IN ({','.join('?' * len(chunk_ids))})",
        chunk_ids,
    ) or []
    return {r["chunk_id"]: r["doc_id"] for r in rows}


def _chunk_texts(db, chunk_ids: list[str]) -> dict[str, str]:
    if not chunk_ids:
        return {}
    rows = db.query(
        f"SELECT chunk_id, text FROM document_chunks WHERE chunk_id IN ({','.join('?' * len(chunk_ids))})",
        chunk_ids,
    ) or []
    return {r["chunk_id"]: r["text"] for r in rows}


def _chunk_vectors(db, chunk_ids: list[str], model) -> dict:
    # Early-return on an empty id list (never emit `IN ()`) and on a missing
    # model identity — a None model here would filter to nothing anyway.
    if not chunk_ids or not model:
        return {}
    import numpy as np  # lazy: module stays import-safe without the extra

    rows = db.query(
        "SELECT chunk_id, vec FROM chunk_vectors WHERE model_id = ? AND chunk_id IN "
        f"({','.join('?' * len(chunk_ids))})",
        [model] + chunk_ids,
    ) or []
    out = {}
    for r in rows:
        out[r["chunk_id"]] = np.frombuffer(r["vec"], dtype="<f4")
    return out
