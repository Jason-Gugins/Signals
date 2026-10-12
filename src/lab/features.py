"""Feature families over the ledger corpus (decide-lab Task 4).

Two families over :func:`src.lab.corpus.extract` rows — the joined doc text
rides the ``text`` field (the 9,600-char gate-state head), ``claims`` the
opt-in persisted payload ``[{text, quote_span, doc_id}, ...]``, ``source``
the raw-store source adapter name:

* :func:`deterministic_features` — cheap, always-available structure and
  vocabulary signals: char/token counts, heading/list line-shape densities,
  TF-IDF cosine vs the market profile's served-problem vocabulary, the
  shipped :func:`src.decide.gates.lexical_overlap` and quote-span stats over
  persisted claims, and doc-source one-hots. The vocabulary comes from the
  profile object :func:`src.intel.market.load_market_profile` returns —
  never hardcoded here. Hand-rolled TF-IDF (sklearn's smooth-idf formula),
  so this family needs numpy only, never sklearn.
* :func:`embedding_features` — the shipped
  :func:`src.retrieval.embedder.get_embedder` on the doc text (384-dim,
  revision-pinned, zero new infra), cached to
  ``data/lab/embeddings/<doc_id>.npy`` so the family is offline forever
  after first encode. A NullEmbedder (or any embed failure) means the
  family is UNAVAILABLE: an honest ``(None, name)``, never an error — the
  same contract as the rerank vector leg.

numpy at module top is deliberate: this is a lab-only module behind the
``signals[lab]`` extra, never imported by the intel pipeline
(tests/test_lab_isolation.py enforces the import graph).
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

import numpy as np

from src.decide.gates import estimate_tokens, lexical_overlap
from src.retrieval.embedder import DIM, get_embedder

DEFAULT_EMBEDDING_CACHE = "data/lab/embeddings"

EMBEDDING_NAME = "embedding"

#: Fixed column order before the per-batch source one-hots. Source columns
#: (``source=<name>``, one per distinct row source, sorted) are appended so
#: the matrix and its names always agree.
DETERMINISTIC_NAMES: tuple[str, ...] = (
    "char_count",
    "token_count",
    "line_count",
    "heading_density",
    "list_density",
    "profile_vocab_hits",
    "profile_tfidf_cosine",
    "lexical_overlap_mean",
    "lexical_overlap_max",
    "claim_count",
    "quote_span_mean_chars",
    "quote_span_frac",
)

# A list item: "-", "*", "+", "bullet", or "1. " / "2)" style markers. The
# digits are bounded so "2026 results" (a year, not an ordinal) never counts.
_LIST_MARKER_RE = re.compile(r"^(?:[-*+\u2022]|\d{1,3}[.)](?:\s|$))")
# An HTML heading tag at line start (raw-store bodies are frequently HTML).
_HEADING_TAG_RE = re.compile(r"^<h[1-6](?:\s|>|/)")


def _is_heading(line: str) -> bool:
    """Line-shape heading heuristic: a markdown ``#`` prefix, an HTML
    ``<hN>`` tag at line start, or an ALL-CAPS line (>= 2 letters, every
    letter uppercase — title-case and sentence-case lines never match)."""
    s = line.strip()
    if not s:
        return False
    if s.startswith("#"):
        return True
    if _HEADING_TAG_RE.match(s.lower()):
        return True
    letters = [c for c in s if c.isalpha()]
    return len(letters) >= 2 and all(c.isupper() for c in letters)


def _is_list_item(line: str) -> bool:
    return bool(_LIST_MARKER_RE.match(line.strip()))


def _profile_vocab(profile: Any) -> list[str]:
    """The served-problem vocabulary from the market profile
    (:func:`src.intel.market.load_market_profile`'s MarketProfile shape):
    every offering's ``served_problem_phrases``, casefolded, deduped,
    first-seen order. An unfilled profile yields an empty vocabulary and
    matches nothing on purpose (the market-profile contract)."""
    phrases: list[str] = []
    seen: set[str] = set()
    offerings = getattr(profile, "offerings", None) or ()
    for offering in offerings:
        for phrase in getattr(offering, "served_problem_phrases", None) or ():
            folded = str(phrase).strip().casefold()
            if folded and folded not in seen:
                seen.add(folded)
                phrases.append(folded)
    return phrases


def _tfidf_cosines(counts: list[list[int]]) -> list[float]:
    """Cosine between each row's TF-IDF vector and the profile vector (tf=1
    for every vocabulary term), over the vocabulary axis.

    ``counts[i][j]`` is vocabulary term j's substring count in row i.
    Smooth idf — sklearn's formula, so the numbers match a TfidfVectorizer:
    ``idf = ln((1 + N) / (1 + df)) + 1`` with df over THIS row batch. Rows
    with no vocabulary hits score 0.0; an empty vocabulary scores all 0.0.
    """
    n = len(counts)
    if n == 0:
        return []
    width = len(counts[0])
    if width == 0:
        return [0.0] * n
    idf = [
        math.log((1.0 + n) / (1.0 + sum(1 for row in counts if row[j] > 0))) + 1.0
        for j in range(width)
    ]
    p_norm = math.sqrt(sum(w * w for w in idf))
    out: list[float] = []
    for row in counts:
        doc = [c * w for c, w in zip(row, idf)]
        d_norm = math.sqrt(sum(x * x for x in doc))
        if d_norm <= 0.0 or p_norm <= 0.0:
            out.append(0.0)
            continue
        dot = sum(a * b for a, b in zip(doc, idf))
        out.append(dot / (d_norm * p_norm))
    return out


def _claims(row: dict) -> list[dict]:
    claims = row.get("claims")
    if not isinstance(claims, list):
        return []
    return [c for c in claims if isinstance(c, dict)]


def _quote_spans(claims: list[dict]) -> list[int]:
    return [
        len(c["quote_span"])
        for c in claims
        if isinstance(c.get("quote_span"), str) and c["quote_span"]
    ]


def deterministic_features(
    rows: list[dict], profile: Any
) -> tuple[np.ndarray, list[str]]:
    """Encode ``rows`` (corpus dicts) into the deterministic family.

    ``profile`` is the object :func:`src.intel.market.load_market_profile`
    returns (only ``offerings[*].served_problem_phrases`` is read). Rows
    without a joined doc text / claims / source encode as zeros for the
    missing inputs (``token_count`` reuses :func:`estimate_tokens`, whose
    contract is never-zero). Returns ``(matrix, names)``: an
    ``(len(rows), len(names))`` float64 array plus the column names —
    :data:`DETERMINISTIC_NAMES` followed by one ``source=<name>`` column per
    distinct row source (sorted).
    """
    texts = [str(row.get("text") or "") for row in rows]
    vocab = _profile_vocab(profile)
    hays = [t.casefold() for t in texts]
    counts = [[hay.count(phrase) for phrase in vocab] for hay in hays]
    cosines = _tfidf_cosines(counts)
    sources = sorted(
        {str(row["source"]) for row in rows if row.get("source") is not None}
    )
    names = [*DETERMINISTIC_NAMES, *(f"source={s}" for s in sources)]

    matrix = np.zeros((len(rows), len(names)), dtype=np.float64)
    for i, (row, text) in enumerate(zip(rows, texts)):
        lines = [line for line in text.split("\n") if line.strip()]
        claims = _claims(row)
        overlaps = [lexical_overlap(str(c.get("text") or ""), text) for c in claims]
        spans = _quote_spans(claims)
        matrix[i, : len(DETERMINISTIC_NAMES)] = [
            float(len(text)),  # char_count
            float(estimate_tokens(text)),  # token_count
            float(len(lines)),  # line_count
            (sum(1 for l in lines if _is_heading(l)) / len(lines)) if lines else 0.0,
            (sum(1 for l in lines if _is_list_item(l)) / len(lines)) if lines else 0.0,
            float(sum(1 for c in counts[i] if c > 0)),  # profile_vocab_hits
            cosines[i],  # profile_tfidf_cosine
            (sum(overlaps) / len(overlaps)) if overlaps else 0.0,
            max(overlaps) if overlaps else 0.0,
            float(len(claims)),  # claim_count
            (sum(spans) / len(spans)) if spans else 0.0,  # quote_span_mean_chars
            (len(spans) / len(claims)) if claims else 0.0,  # quote_span_frac
        ]
        source = row.get("source")
        if source is not None:
            j = sources.index(str(source))
            matrix[i, len(DETERMINISTIC_NAMES) + j] = 1.0
    return matrix, names


def _load_cached(cache_dir: Path, doc_id: str) -> np.ndarray | None:
    path = cache_dir / f"{doc_id}.npy"
    if not path.exists():
        return None
    try:
        vec = np.load(path)
    except Exception:  # corrupt/unreadable cache file: a miss, not an error
        return None
    if isinstance(vec, np.ndarray) and vec.shape == (DIM,):
        return vec.astype(np.float32)
    return None  # wrong-shape cache (stale dims): a miss


def embedding_features(
    rows: list[dict],
    *,
    embedder: Any = None,
    cache_dir: str | Path = DEFAULT_EMBEDDING_CACHE,
) -> tuple[np.ndarray | None, str]:
    """Encode ``rows`` into the embedding family: 384-dim
    all-MiniLM-L6-v2 vectors of the joined doc text, cached per ``doc_id``
    under ``cache_dir`` (cache-forever — offline once cached, the same
    posture as the backfill claim cache).

    ``embedder`` defaults to :func:`src.retrieval.embedder.get_embedder`
    (injectable for test doubles). Duplicate doc_ids embed once and share
    the vector; rows without a ``doc_id`` embed every call and are never
    cached. Returns ``(matrix, "embedding")``, or ``(None, "embedding")``
    when ANY row needing a fresh encode cannot get one (NullEmbedder,
    failed embed, wrong dimensionality) — the family is honestly
    unavailable, never an error, and never a silent mix of cached and
    missing vectors.
    """
    if embedder is None:
        embedder = get_embedder()

    n = len(rows)
    if n == 0:
        return np.zeros((0, DIM), dtype=np.float32), EMBEDDING_NAME

    cache = Path(cache_dir)
    vectors: list[np.ndarray | None] = [None] * n
    first_row_of_doc: dict[str, int] = {}
    pending_idx: list[int] = []
    pending_texts: list[str] = []

    for i, row in enumerate(rows):
        doc_id = str(row.get("doc_id") or "") or None
        if doc_id and doc_id in first_row_of_doc:
            continue  # duplicate doc in this batch: copy after encode/cache
        if doc_id:
            first_row_of_doc[doc_id] = i
            cached = _load_cached(cache, doc_id)
            if cached is not None:
                vectors[i] = cached
                continue
        pending_idx.append(i)
        pending_texts.append(str(row.get("text") or ""))

    if pending_idx:
        fresh = embedder.embed(pending_texts)
        if not isinstance(fresh, list) or len(fresh) != len(pending_idx):
            return None, EMBEDDING_NAME
        encoded: dict[str, np.ndarray] = {}
        for i, vec in zip(pending_idx, fresh):
            try:
                arr = np.asarray(vec, dtype=np.float32)
            except (TypeError, ValueError):
                return None, EMBEDDING_NAME
            if arr.shape != (DIM,):
                return None, EMBEDDING_NAME
            vectors[i] = arr
            doc_id = str(rows[i].get("doc_id") or "") or None
            if doc_id:
                encoded[doc_id] = arr
        # Cache writes are best-effort: a read-only cache dir degrades to
        # re-encoding next call, never to a family failure.
        try:
            cache.mkdir(parents=True, exist_ok=True)
            for doc_id, arr in encoded.items():
                np.save(cache / f"{doc_id}.npy", arr)
        except OSError:
            pass

    for i, row in enumerate(rows):
        if vectors[i] is None:  # duplicate-doc rows copy their doc's vector
            doc_id = str(row.get("doc_id") or "") or None
            vectors[i] = vectors[first_row_of_doc[doc_id]]

    return np.stack(vectors).astype(np.float32), EMBEDDING_NAME
