"""Tests for optional cross-encoder rerank (P3, donsetch-inspired)."""
from __future__ import annotations

import pytest


def test_noop_scorer_is_default_when_deps_missing():
    """Without onnxruntime installed, get_scorer() returns a no-op that
    scores everything None — callers must treat None as 'no adjustment'."""
    from src.signals import rerank

    scorer = rerank.get_scorer()
    # In the test env (no [rerank] extra installed), the scorer is the no-op.
    assert isinstance(scorer, rerank.NullScorer)


def test_null_scorer_returns_none_for_every_pair():
    from src.signals.rerank import NullScorer

    s = NullScorer()
    assert s.score_pairs("acme raised series b", [("t1", "s1"), ("t2", "s2")]) is None


def test_relevance_floor_drops_below_threshold():
    from src.signals.rerank import apply_relevance_floor

    items = [
        {"title": "Acme raises $12M Series B", "summary": "growth"},
        {"title": "Acme customer raised funding", "summary": "tangential"},
    ]
    scores = [0.9, 0.2]
    kept = apply_relevance_floor(items, scores, floor=0.35)
    assert len(kept) == 1
    assert kept[0]["title"].startswith("Acme raises")


def test_relevance_floor_with_no_scores_keeps_everything():
    """scores=None means no model ran — never drop anything."""
    from src.signals.rerank import apply_relevance_floor

    items = [{"title": "a"}, {"title": "b"}]
    kept = apply_relevance_floor(items, None, floor=0.9)
    assert kept == items


def test_scores_attached_as_evidence_never_change_keys():
    from src.signals.rerank import attach_relevance_evidence

    items = [{"title": "t", "summary": "s", "natural_key": "abc"}]
    out = attach_relevance_evidence(items, [0.7])
    assert out[0]["evidence_data"]["relevance"] == 0.7
    assert out[0]["natural_key"] == "abc"  # stability guarantee


def test_attach_relevance_none_scores_leaves_items_untouched():
    from src.signals.rerank import attach_relevance_evidence

    items = [{"title": "t", "summary": "s", "natural_key": "abc"}]
    out = attach_relevance_evidence(items, None)
    assert out[0] == {"title": "t", "summary": "s", "natural_key": "abc"}


def test_attach_relevance_does_not_mutate_input():
    from src.signals.rerank import attach_relevance_evidence

    items = [{"title": "t", "summary": "s", "natural_key": "abc"}]
    attach_relevance_evidence(items, [0.5])
    assert "evidence_data" not in items[0]


def test_attach_relevance_preserves_existing_evidence_data():
    from src.signals.rerank import attach_relevance_evidence

    items = [
        {
            "title": "t",
            "natural_key": "k",
            "evidence_data": {"serp_position": 3},
        }
    ]
    out = attach_relevance_evidence(items, [0.42])
    assert out[0]["evidence_data"]["serp_position"] == 3
    assert out[0]["evidence_data"]["relevance"] == 0.42


def test_module_imports_without_ml_deps():
    """The module must import cleanly with zero ML deps installed."""
    import importlib

    from src.signals import rerank

    importlib.reload(rerank)
    assert rerank.MODEL_REPO == "Xenova/ms-marco-MiniLM-L-6-v2"


@pytest.mark.rerank_live
def test_onnx_scorer_scores_pairs_when_extra_installed():
    pytest.importorskip("onnxruntime")
    pytest.importorskip("tokenizers")
    pytest.importorskip("huggingface_hub")
    from src.signals.rerank import OnnxScorer

    scorer = OnnxScorer()
    scores = scorer.score_pairs(
        "acme raised series b", [("Acme closes $12M Series B round", "funding news")]
    )
    assert scores is not None
    assert len(scores) == 1
    assert 0.0 <= scores[0] <= 1.0
