"""Optional cross-encoder relevance reranking (donsetch-inspired).

Graceful degradation is the contract: if onnxruntime/tokenizers are not
installed, or the model download fails, reranking is skipped and callers
get None scores — the pipeline runs exactly as before. This module is
import-safe WITHOUT the [rerank] extra: no module-level ML imports.
"""
from __future__ import annotations

import copy
import logging
import math
from typing import Optional, Protocol

logger = logging.getLogger(__name__)

# Overridable in case the upstream repo layout drifts (see plan risks).
MODEL_REPO = "Xenova/ms-marco-MiniLM-L-6-v2"
MODEL_FILE = "onnx/model_quantized.onnx"
TOKENIZER_FILE = "tokenizer.json"
# Pinned repo revision (supply-chain: matches donsetch's SHA256-pinning practice).
# Verify before bumping: both files exist at this revision (checked 2026-09-01).
MODEL_REVISION: Optional[str] = "a09144355adeed5f58c8ed011d209bf8ee5a1fec"
DEFAULT_FLOOR = 0.35
MAX_SEQ_LEN = 256


class RelevanceScorer(Protocol):
    def score_pairs(
        self, query: str, docs: list[tuple[str, str]]
    ) -> Optional[list[float]]: ...


class NullScorer:
    """Scores nothing — rerank disabled. Callers treat None as no adjustment."""

    def score_pairs(self, query: str, docs: list[tuple[str, str]]) -> None:
        return None


class OnnxScorer:
    """ms-marco-MiniLM-L-6-v2 cross-encoder via onnxruntime (lazy singleton)."""

    def __init__(self, model_repo: str = MODEL_REPO) -> None:
        self._session = None
        self._tokenizer = None
        self._model_repo = model_repo

    def _ensure_loaded(self) -> bool:
        if self._session is not None:
            return True
        try:
            import onnxruntime as ort
            from huggingface_hub import hf_hub_download
            from tokenizers import Tokenizer

            kwargs = {} if MODEL_REVISION is None else {"revision": MODEL_REVISION}
            tok_path = hf_hub_download(
                self._model_repo, TOKENIZER_FILE, **kwargs
            )
            onnx_path = hf_hub_download(self._model_repo, MODEL_FILE, **kwargs)
            tok = Tokenizer.from_file(tok_path)
            tok.enable_truncation(max_length=MAX_SEQ_LEN)
            tok.enable_padding(direction="right")
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 2  # don't starve the scheduler
            self._session = ort.InferenceSession(onnx_path, opts)
            self._tokenizer = tok
            return True
        except Exception as exc:
            logger.warning("rerank model unavailable, reranking disabled: %s", exc)
            self._session = None
            self._tokenizer = None
            return False

    def score_pairs(
        self, query: str, docs: list[tuple[str, str]]
    ) -> Optional[list[float]]:
        if not self._ensure_loaded():
            return None
        if not docs:
            return []
        # docs are (title, summary) pairs — join into one text per doc so the
        # pair fed to the tokenizer is (query, doc_text) with plain strings.
        enc = self._tokenizer.encode_batch([(query, f"{t} — {s}" if s else t) for t, s in docs])
        input_ids = [e.ids for e in enc]
        attention_mask = [e.attention_mask for e in enc]
        type_ids = [e.type_ids for e in enc]

        import numpy as np  # onnxruntime depends on numpy; lazy anyway

        feeds = {
            "input_ids": np.array(input_ids, dtype=np.int64),
            "attention_mask": np.array(attention_mask, dtype=np.int64),
            "token_type_ids": np.array(type_ids, dtype=np.int64),
        }
        output_names = [o.name for o in self._session.get_outputs()]
        run_keys = {k: v for k, v in feeds.items() if k in [
            i.name for i in self._session.get_inputs()
        ]}
        logits = self._session.run(output_names, run_keys)[0].reshape(-1)
        # Cross-encoder logits over CLS -> sigmoid to a 0..1 relevance score.
        # Numerically stable sigmoid: plain 1/(1+exp(-x)) overflows for x < -709.
        out = []
        for x in logits:
            x = float(x)
            if x >= 0:
                out.append(1.0 / (1.0 + math.exp(-x)))
            else:
                e = math.exp(x)
                out.append(e / (1.0 + e))
        return out


# Module-level memoized scorer: constructing OnnxScorer is cheap, but the
# underlying _ensure_loaded pays a hf_hub round-trip + session build — pay
# that ONCE per process, not once per parse. Double-checked locking (the
# collector thread and the watch loop can both reach get_scorer()).
_scorer_lock = __import__("threading").Lock()
_scorer_instance: Optional[RelevanceScorer] = None


def get_scorer() -> RelevanceScorer:
    """Return the best available scorer; NullScorer when deps/model absent.

    Memoized: one scorer instance per process (the ONNX session and tokenizer
    load once, then every parse reuses them).
    """
    global _scorer_instance
    if _scorer_instance is not None:
        return _scorer_instance
    with _scorer_lock:
        if _scorer_instance is not None:
            return _scorer_instance
        try:
            import onnxruntime  # noqa: F401
        except ImportError:
            _scorer_instance = NullScorer()
            return _scorer_instance
        _scorer_instance = OnnxScorer()
        return _scorer_instance


def apply_relevance_floor(
    items: list[dict], scores: Optional[list[float]], *, floor: float
) -> list[dict]:
    """Drop items whose relevance score is below floor.

    scores=None means no model ran — keep ALL items (no model, no dropping).
    """
    if scores is None:
        return list(items)
    return [
        item for item, score in zip(items, scores) if score is not None and score >= floor
    ]


def attach_relevance_evidence(
    items: list[dict], scores: Optional[list[float]]
) -> list[dict]:
    """Attach evidence_data['relevance'] without touching natural_key.

    Never mutates the input: items are copied (shallow item copy; evidence_data
    is copied when present, created when absent).
    """
    if scores is None:
        return copy.deepcopy(items)
    out: list[dict] = []
    for item, score in zip(items, scores):
        if score is None:
            out.append(copy.deepcopy(item))
            continue
        new_item = {**item}
        evidence = dict(new_item.get("evidence_data") or {})
        evidence["relevance"] = score
        new_item["evidence_data"] = evidence
        out.append(new_item)
    return out
