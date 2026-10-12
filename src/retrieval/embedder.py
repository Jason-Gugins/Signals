"""Local sentence embeddings (Xenova/all-MiniLM-L6-v2, ONNX quantized).

Same contract as src/signals/rerank.py: import-safe without the [rerank]
extra, lazy singleton, revision-pinned download, and ANY failure degrades to
NullEmbedder (embed() -> None) — callers treat None as "leg unavailable",
never as an error. Mean pooling over the attention mask, L2-normalised, so
dot product == cosine similarity.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional, Protocol

logger = logging.getLogger(__name__)

MODEL_REPO = "Xenova/all-MiniLM-L6-v2"
MODEL_FILE = "onnx/model_quantized.onnx"
TOKENIZER_FILE = "tokenizer.json"
# Pin the revision after the first successful download, exactly as
# src/signals/rerank.py does (supply-chain posture: same hf_hub pattern).
# Verified 2026-10-10: both files exist at this revision; live embed()
# returned 384-dim vectors.
MODEL_REVISION: Optional[str] = "751bff37182d3f1213fa05d7196b954e230abad9"
DIM = 384
MAX_SEQ_LEN = 256  # tokenizer truncation; matches the reranker's budget
# Rows per session.run() in embed(). ONNX preallocates the arena for the
# WHOLE request's activations: encoding an entire input list in one run had
# a single ~2.6k-chunk document request a 174 GB allocation (observed during
# the 2026-10 retrieval A/B re-index; the indexer caught it as an error).
# 32 rows bounds every request; batches are concatenated IN INPUT ORDER.
EMBED_BATCH = 32


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> Optional[list[list[float]]]: ...


class NullEmbedder:
    """Embeds nothing — vector leg disabled. Callers treat None as unavailable."""

    def embed(self, texts: list[str]) -> None:
        return None


class OnnxEmbedder:
    """all-MiniLM-L6-v2 bi-encoder via onnxruntime (lazy singleton)."""

    def __init__(
        self,
        model_repo: str = MODEL_REPO,
        revision: Optional[str] = MODEL_REVISION,
    ) -> None:
        self._session = None
        self._tokenizer = None
        self._model_repo = model_repo
        self._revision = revision

    @property
    def model(self) -> str:
        """Identity used by the indexer, the search model filters AND the
        test fakes (`.model` attribute) — ONE accessor everywhere. A
        `.model`-vs-`model_id()` mismatch here makes the vector leg silently
        query `model_id = NULL` and return nothing (review finding)."""
        return f"{self._model_repo}@{self._revision or 'unpinned'}"

    def _ensure_loaded(self) -> bool:
        if self._session is not None:
            return True
        try:
            import onnxruntime as ort
            from huggingface_hub import hf_hub_download
            from tokenizers import Tokenizer

            kwargs = {} if self._revision is None else {"revision": self._revision}
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
            logger.warning("embedder model unavailable, vector leg disabled: %s", exc)
            self._session = None
            self._tokenizer = None
            return False

    def embed(self, texts: list[str]) -> Optional[list[list[float]]]:
        if not texts:
            return []
        if not self._ensure_loaded():
            return None
        try:
            # numpy is imported lazily (onnxruntime depends on it), NOT stashed
            # on self by _ensure_loaded: test doubles inject _session/_tokenizer
            # directly, short-circuiting _ensure_loaded entirely.
            import numpy as np

            names = [i.name for i in self._session.get_inputs()]
            output_names = [o.name for o in self._session.get_outputs()]
            pooled_parts = []
            for start in range(0, len(texts), EMBED_BATCH):
                batch = texts[start : start + EMBED_BATCH]
                enc = self._tokenizer.encode_batch(batch)
                feeds = {
                    "input_ids": np.array([e.ids for e in enc], dtype=np.int64),
                    "attention_mask": np.array(
                        [e.attention_mask for e in enc], dtype=np.int64
                    ),
                    "token_type_ids": np.array([e.type_ids for e in enc], dtype=np.int64),
                }
                run_keys = {k: v for k, v in feeds.items() if k in names}
                hidden = self._session.run(output_names, run_keys)[0]
                # Mean pool over REAL tokens (attention mask — padded positions
                # contribute nothing), per batch; parts concatenated IN ORDER.
                mask = feeds["attention_mask"].astype("float32")[..., None]
                pooled_parts.append(
                    (hidden * mask).sum(axis=1) / mask.sum(axis=1).clip(min=1e-9)
                )
            # texts is non-empty here, so pooled_parts has at least one part.
            pooled = np.concatenate(pooled_parts, axis=0)
            # L2-normalise so dot == cosine.
            norms = np.linalg.norm(pooled, axis=1, keepdims=True).clip(min=1e-12)
            pooled = (pooled / norms).astype("float32")
            return [row.tolist() for row in pooled]
        except Exception as exc:
            logger.warning("embed() failed: %s", exc)
            return None


# Module-level memoized embedder: constructing OnnxEmbedder is cheap, but the
# underlying _ensure_loaded pays a hf_hub round-trip + session build — pay
# that ONCE per process, not once per embed. Double-checked locking (the
# indexer and the watch loop can both reach get_embedder()).
_embedder_lock = threading.Lock()
_embedder_instance: Optional[Embedder] = None


def get_embedder() -> Embedder:
    """Return the best available embedder; NullEmbedder when deps/model absent.

    Memoized: one embedder instance per process (the ONNX session and
    tokenizer load once, then every index/search reuses them).
    """
    global _embedder_instance
    if _embedder_instance is not None:
        return _embedder_instance
    with _embedder_lock:
        if _embedder_instance is not None:
            return _embedder_instance
        try:
            import onnxruntime  # noqa: F401
        except ImportError:
            _embedder_instance = NullEmbedder()
            return _embedder_instance
        _embedder_instance = OnnxEmbedder()
        return _embedder_instance
