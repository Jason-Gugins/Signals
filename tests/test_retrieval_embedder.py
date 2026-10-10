"""Embedder: NullEmbedder when deps absent; ONNX math verified with doubles."""
from __future__ import annotations

import pytest

from src.retrieval import embedder


class FakeTokenizer:
    """Double honouring the real tokenizer's PADDED-BATCH contract.

    Production _ensure_loaded configures enable_truncation(max_length=256) +
    enable_padding(direction="right"), so encode_batch always returns
    equal-length ids/attention_mask per batch — right-padded with id 0 /
    mask 0 (numpy 2.x raises on ragged arrays). One token per char,
    capped at 8 — enough to exercise pooling.
    """

    def enable_truncation(self, max_length):
        self.max_length = max_length

    def encode_batch(self, texts):
        seqs = []
        for pair in texts:
            text = pair[0] if isinstance(pair, tuple) else pair
            ids = [min(1 + ord(c) % 99, 100) for c in text[:8]] or [1]
            seqs.append(ids)
        width = max(len(ids) for ids in seqs)
        out = []
        for ids in seqs:
            pad = width - len(ids)
            out.append(type("E", (), {
                "ids": ids + [0] * pad,
                "attention_mask": [1] * len(ids) + [0] * pad,
                "type_ids": [0] * width,
            })())
        return out


class FakeSession:
    def __init__(self):
        self.calls = 0

    def get_outputs(self):
        return [type("O", (), {"name": "last_hidden_state"})()]

    def get_inputs(self):
        return [type("I", (), {"name": n})() for n in
                ("input_ids", "attention_mask", "token_type_ids")]

    def run(self, names, feeds):
        import numpy as np
        self.calls += 1
        ids = feeds["input_ids"]
        mask = feeds["attention_mask"]
        # deterministic "embedding": mean of id values over masked positions
        summed = (ids * mask).sum(axis=1, keepdims=True).astype("float32")
        counts = mask.sum(axis=1, keepdims=True).astype("float32")
        hidden = np.repeat(summed / counts, ids.shape[1], axis=1)[..., None]
        hidden = np.broadcast_to(hidden, (*ids.shape, 4)).copy()
        return [hidden]


def test_null_embedder_returns_none():
    assert embedder.NullEmbedder().embed(["hello"]) is None


def test_model_identity_string():
    emb = embedder.OnnxEmbedder(model_repo=embedder.MODEL_REPO, revision="deadbeef")
    assert emb.model == f"{embedder.MODEL_REPO}@deadbeef"


def test_onnx_embedder_mean_pool_and_normalize():
    emb = embedder.OnnxEmbedder(model_repo="fake/repo", revision=None)
    emb._session = FakeSession()
    emb._tokenizer = FakeTokenizer()
    vecs = emb.embed(["hiring head of sales", "b"])
    assert vecs is not None and len(vecs) == 2
    for v in vecs:
        assert len(v) == 4          # the FAKE emits dim 4 — pooling is shape-agnostic
        assert abs(sum(x * x for x in v) - 1.0) < 1e-5   # L2-normalised
    # identical texts -> identical vectors
    again = emb.embed(["hiring head of sales"])
    assert again[0] == vecs[0]
    # the real-model pin: the fake's 4 is NOT the production dimensionality
    assert embedder.DIM == 384


def test_embedder_session_failure_degrades():
    emb = embedder.OnnxEmbedder(model_repo="fake/repo", revision=None)
    # _ensure_loaded fails (no real hub/session) -> embed returns None, never raises
    assert emb.embed(["x"]) is None


def test_module_imports_without_ml_deps():
    """The module must import cleanly with zero ML deps installed."""
    import importlib

    importlib.reload(embedder)
    assert embedder.MODEL_REPO == "Xenova/all-MiniLM-L6-v2"


def _onnxruntime_installed() -> bool:
    try:
        import onnxruntime  # noqa: F401

        return True
    except ImportError:
        return False


def test_get_embedder_degrades_by_import_presence():
    """Without onnxruntime installed, get_embedder() returns the null embedder
    whose embed() is None — callers treat None as 'leg unavailable'. (When the
    [rerank] extra IS installed, get_embedder() returns OnnxEmbedder —
    construction is lazy, no download happens here; the live path is the
    rerank_live-style manual check.)"""
    emb = embedder.get_embedder()
    if _onnxruntime_installed():
        assert isinstance(emb, embedder.OnnxEmbedder)
    else:
        assert isinstance(emb, embedder.NullEmbedder)


def test_get_embedder_is_memoized():
    # one instance per process: the hf_hub round-trip + session build is paid once
    assert embedder.get_embedder() is embedder.get_embedder()


@pytest.mark.parametrize("dim", [embedder.DIM])
def test_dim_constant_is_384(dim):
    assert dim == 384
