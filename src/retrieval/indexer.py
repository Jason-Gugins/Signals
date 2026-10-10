"""Index the stored corpus: doc bodies -> extracted text -> chunks -> FTS + vectors.

Explicit, budgeted, resumable. Never raises past a per-doc skip (a bad body
is counted in `skipped_no_text` / `errors`, never fatal) — an index build that
half-succeeds is still a useful index, and the next run finishes it.

Ledger contract: an `index_state` row is written for every doc the indexer
DECIDES — 'indexed' on success, 'no_text' for empty/binary bodies (coverage
counts both as covered, not as a permanent gap — migration v9 review
finding). A doc that FAILS (embed error, mid-insert exception) gets NO row:
the ledger records decisions, not attempts, so the next run retries failed
docs. The per-doc except handler deletes the failed doc's partial chunk rows
to match that contract — without the cleanup, committed partial rows would
make the next run's existing-chunks check short-circuit as skipped_indexed
and lock the doc in as permanently partial.
"""
from __future__ import annotations

import struct
from typing import Optional

from loguru import logger

from src.core.db import Database
from src.core.rawstore import RawStore
from src.core.textutil import html_to_text, stable_id
from src.retrieval.chunker import chunk_text

#: Extracted text shorter than this is not indexed (nothing citable left).
#: The plan sketch pinned 200, but its own contract tests (Step 1) index
#: one-sentence 41- and 53-char pages — the tests are the contract — so the
#: shipped floor only rejects remnants too short to carry a citable quote.
MIN_TEXT_CHARS = 16


def _decode(body) -> str:
    """Stored body bytes -> text; never raises (mirrors intel.py::_decode_body)."""
    if body is None:
        return ""
    if isinstance(body, (bytes, bytearray)):
        return bytes(body).decode("utf-8", errors="replace")
    return str(body)


def _extract_text(doc) -> str:
    ctype = (getattr(doc, "content_type", "") or "").lower()
    raw = _decode(getattr(doc, "body", None))
    if "html" in ctype:
        return html_to_text(raw) or ""
    return raw


def _mark_state(db: Database, doc_id: str, domain: str, model: str, state: str) -> None:
    db.execute(
        "INSERT OR REPLACE INTO index_state(doc_id, domain, model_id, state, checked_at)"
        " VALUES (?,?,?,?,datetime('now'))",
        (doc_id, domain, model, state),
    )


def _fts_delete(db: Database, chunk_ids: list[str]) -> None:
    for cid in chunk_ids:
        db.execute("DELETE FROM chunks_fts WHERE chunk_id = ?", (cid,))


def _delete_doc_chunks(db: Database, doc_id: str, model: str) -> None:
    """Delete one doc's chunk rows for ONE model (vectors + FTS + chunks).

    chunk_vectors has no doc_id column — resolve the chunk_ids first, then
    delete the rows they key, then the chunks themselves (never emit `IN ()`).
    """
    chunk_ids = [
        r["chunk_id"]
        for r in db.query(
            "SELECT chunk_id FROM document_chunks WHERE doc_id = ? AND model_id = ?",
            (doc_id, model),
        )
    ]
    if chunk_ids:
        ph = ",".join("?" * len(chunk_ids))
        db.execute(f"DELETE FROM chunk_vectors WHERE chunk_id IN ({ph})", chunk_ids)
        db.execute(f"DELETE FROM chunks_fts WHERE chunk_id IN ({ph})", chunk_ids)
    db.execute(
        "DELETE FROM document_chunks WHERE doc_id = ? AND model_id = ?",
        (doc_id, model),
    )


def index_domain(
    db: Database,
    raw_store: RawStore,
    domain: str,
    *,
    embedder: Optional[object] = None,
    chunk_tokens: int = 500,
    overlap_tokens: int = 60,
    limit: Optional[int] = None,
) -> dict:
    """Index one domain's stored documents; returns stats. Idempotent per (doc, model).

    A doc already indexed under the SAME model is skipped; re-indexed under a
    NEW model, its old chunks/vectors/FTS rows are deleted first. Per-doc
    failures are counted in `errors` and never raised past.
    """
    # Database.execute / executemany commit per statement / batch — no
    # explicit commit anywhere here.
    model = getattr(embedder, "model", None)  # ONE identity accessor: indexer,
    # coverage ledger and the search-leg model filters all read `.model`.
    stats: dict = {
        "docs_seen": 0,
        "docs_indexed": 0,
        "chunks": 0,
        "skipped_no_text": 0,
        "skipped_indexed": 0,
        "errors": 0,
        "model": model,
    }
    docs = list(raw_store.iter_docs(domain=domain))
    docs.reverse()  # newest first: --limit budgets the freshest docs
    if limit:
        docs = docs[: int(limit)]
    stats["docs_seen"] = len(docs)
    if model is None:
        logger.warning(
            "index: embedder has no model identity; nothing indexed for {}", domain
        )
        return stats
    for meta in docs:
        doc_id = str(getattr(meta, "doc_id", "") or "")
        try:
            existing = db.one(
                "SELECT COUNT(*) AS n FROM document_chunks WHERE doc_id = ? AND model_id = ?",
                (doc_id, model),
            )
            if existing and int(existing["n"]) > 0:
                stats["skipped_indexed"] += 1
                continue
            full = raw_store.get(doc_id)
            text = _extract_text(full) if full is not None else ""
            if len(text.strip()) < MIN_TEXT_CHARS:
                stats["skipped_no_text"] += 1
                _mark_state(db, doc_id, domain, model, "no_text")
                continue
            chunks = chunk_text(
                text, target_tokens=chunk_tokens, overlap_tokens=overlap_tokens
            )
            if not chunks:
                stats["skipped_no_text"] += 1
                _mark_state(db, doc_id, domain, model, "no_text")
                continue
            vecs = embedder.embed([c.text for c in chunks])
            if not vecs or len(vecs) != len(chunks):
                stats["errors"] += 1
                continue
            # re-index of a doc under a NEW model: drop the old rows first.
            # chunk_vectors has no doc_id column — the delete goes strictly
            # through the old chunk ids (never emit `IN ()`).
            old = [
                r["chunk_id"]
                for r in db.query(
                    "SELECT chunk_id FROM document_chunks WHERE doc_id = ?", (doc_id,)
                )
            ]
            db.execute("DELETE FROM document_chunks WHERE doc_id = ?", (doc_id,))
            if old:
                db.execute(
                    f"DELETE FROM chunk_vectors WHERE chunk_id IN "
                    f"({','.join('?' * len(old))})",
                    old,
                )
            _fts_delete(db, old)
            # One batched write per table (Database.executemany commits once):
            # a doc costs ~3 commits instead of ~3 per chunk over the
            # per-chunk autocommitting loop (backfill perf — review finding).
            chunk_rows: list[tuple] = []
            vec_rows: list[tuple] = []
            fts_rows: list[tuple] = []
            for chunk, vec in zip(chunks, vecs):
                cid = stable_id(doc_id, str(chunk.ordinal), model, length=32)
                chunk_rows.append(
                    (
                        cid,
                        doc_id,
                        domain,
                        chunk.ordinal,
                        chunk.char_start,
                        chunk.char_end,
                        chunk.text,
                        chunk.token_est,
                        model,
                    )
                )
                vec_rows.append(
                    (cid, model, len(vec), struct.pack(f"<{len(vec)}f", *vec))
                )
                fts_rows.append((chunk.text, cid))
            db.executemany(
                "INSERT INTO document_chunks(chunk_id, doc_id, domain, ordinal,"
                " char_start, char_end, text, token_est, model_id, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,datetime('now'))",
                chunk_rows,
            )
            db.executemany(
                "INSERT INTO chunk_vectors(chunk_id, model_id, dim, vec)"
                " VALUES (?,?,?,?)",
                vec_rows,
            )
            db.executemany(
                "INSERT INTO chunks_fts(text, chunk_id) VALUES (?, ?)",
                fts_rows,
            )
            _mark_state(db, doc_id, domain, model, "indexed")
            stats["docs_indexed"] += 1
            stats["chunks"] += len(chunks)
        except Exception:
            logger.exception("index: doc {} failed", doc_id)
            # The run may have committed PARTIAL chunk rows before the
            # failure (inserts commit per statement) — without cleanup the
            # next run's existing-chunks check short-circuits as
            # skipped_indexed and the doc stays permanently partial. Delete
            # this doc's rows for the CURRENT model so the next run retries
            # it cleanly. Best-effort: cleanup never raises past the doc.
            try:
                _delete_doc_chunks(db, doc_id, model)
            except Exception:
                logger.warning(
                    "index: post-failure cleanup incomplete for doc {}", doc_id
                )
            stats["errors"] += 1
    return stats
