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


class DistinctFakeSession(FakeSession):
    """FakeSession variant whose per-row output DIFFERS per hidden dim
    (row's mean id + dim offset), so distinct texts keep distinct post-norm
    vectors — the base fake's constant rows all L2-normalise to the same
    unit vector, which would make any order check vacuous. Also records
    each run's batch size so tests can pin the cap itself."""

    def __init__(self):
        super().__init__()
        self.batch_sizes = []

    def run(self, names, feeds):
        import numpy as np

        self.calls += 1
        ids = feeds["input_ids"]
        mask = feeds["attention_mask"]
        self.batch_sizes.append(ids.shape[0])
        summed = (ids * mask).sum(axis=1, keepdims=True).astype("float32")
        counts = mask.sum(axis=1, keepdims=True).astype("float32")
        base = summed / counts  # (rows, 1): the row's mean masked id
        per_dim = base + np.arange(1, 5, dtype="float32")  # (rows, 4)
        hidden = np.broadcast_to(
            per_dim[:, None, :], (ids.shape[0], ids.shape[1], 4)
        ).copy()
        return [hidden]


class OomOnSecondRunFakeSession(FakeSession):
    """First session.run succeeds, second raises — models the arena blowup
    landing mid-document (batch 2 of N). Delegates the successful call to
    FakeSession.run, which owns the calls counter."""

    def run(self, names, feeds):
        if self.calls >= 1:
            raise MemoryError("onnxruntime: requested a 174 GB arena allocation")
        return FakeSession.run(self, names, feeds)


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


def test_embed_batch_cap_is_32():
    # pin: the cap that kills the 174 GB arena request (a ~2.6k-chunk doc
    # was encoded in ONE session.run during the 2026-10 retrieval A/B)
    assert embedder.EMBED_BATCH == 32


def test_embed_slices_into_batches_and_keeps_order():
    emb = embedder.OnnxEmbedder(model_repo="fake/repo", revision=None)
    session = DistinctFakeSession()
    emb._session = session
    emb._tokenizer = FakeTokenizer()
    # 100 texts: 99 single chars (pairwise-distinct id means) plus one
    # two-char text (non-integer mean) — every text must embed distinctly,
    # so a batch-order mismatch cannot hide.
    texts = [chr(33 + i) for i in range(99)] + [chr(33) + chr(34)]
    assert len(texts) == 100

    vecs = emb.embed(texts)

    assert vecs is not None and len(vecs) == 100
    assert session.calls == 4                      # ceil(100/32) session.runs
    assert session.batch_sizes == [32, 32, 32, 4]  # the cap itself
    assert len({tuple(v) for v in vecs}) == 100     # all distinct (order-observable)
    # results are IN INPUT ORDER: batched[i] == text i's solo embedding
    solo = embedder.OnnxEmbedder(model_repo="fake/repo", revision=None)
    solo._session = DistinctFakeSession()
    solo._tokenizer = FakeTokenizer()
    for i, text in enumerate(texts):
        assert solo.embed([text]) == [vecs[i]]


def test_embed_batch_failure_degrades_whole_call_to_none():
    emb = embedder.OnnxEmbedder(model_repo="fake/repo", revision=None)
    emb._session = OomOnSecondRunFakeSession()
    emb._tokenizer = FakeTokenizer()
    # 64 texts = 2 batches at cap 32: batch 1 succeeds, batch 2 OOMs.
    # Existing contract: ANY failure degrades the WHOLE call to None.
    assert emb.embed([f"chunk {i}" for i in range(64)]) is None
