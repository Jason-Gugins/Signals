"""Task 4 (decide-lab): the baseline battery + embedding features.

Two feature families over :func:`src.lab.corpus.extract` rows (the joined
doc text rides the ``text`` field):

* deterministic — char/token counts, heading/list line-shape densities,
  TF-IDF cosine vs the market profile's served-problem vocabulary (loaded
  via the REAL :func:`src.intel.market.load_market_profile` — the vocabulary
  is never hardcoded in features.py), ``lexical_overlap`` /
  quote-span stats over persisted claims, and doc-source one-hots. Every
  number is computed exactly here on hand-checkable rows.
* embedding — :func:`src.retrieval.embedder.get_embedder` on the doc text
  (384-dim), cached to ``data/lab/embeddings/<doc_id>.npy`` (offline once
  cached). A NullEmbedder means the family is UNAVAILABLE: honest
  ``(None, name)``, never an error.

And :func:`src.lab.baselines.score_baselines`: elastic-net logistic on each
family separately, then combined; AUROC + PR-AUC on a chronological split
(rows sorted by generated_at/package timestamps, later rows in the TEST
half — deployment predicts future runs; positional first-half/second-half
fallback when no timestamps parse). The mandate line every report hangs on:
discovered questions must beat the best single family.

Gating mirrors the rerank pattern: numpy guards the module import (features
needs it), sklearn-needing tests importorskip so the lane skips gracefully
without the ``signals[lab]`` extra.
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")  # src.lab.features imports numpy at module top

from src.intel.market import load_market_profile  # noqa: E402
from src.lab import baselines, features  # noqa: E402
from src.retrieval.embedder import DIM, NullEmbedder  # noqa: E402

# --------------------------------------------------------------- test corpus

# Three hand-checkable doc rows. Line shapes:
#   R1: 5 non-empty lines — 2 headings ("# ..." prefix, ALL-CAPS line),
#       2 list items ("- ..."), 1 plain sentence; 2 persisted claims.
#   R2: 2 plain lines, no claims; hits 2 of 3 vocabulary phrases.
#   R3: 3 lines — 1 ALL-CAPS heading, 2 numbered list items; no vocab hits.
R1_TEXT = (
    "# Orbital Billing Launch\n"
    "ACME SHIPS ORBITAL BILLING 2.0\n"
    "- security investigation upgraded\n"
    "- risky behavior alerts\n"
    "Acme publishes the changelog weekly.\n"
)
R2_TEXT = (
    "Acme raises Series F\n"
    "Acme said security investigation demand grew after enterprise ai systems spread.\n"
)
R3_TEXT = "CHANGELOG\n1. orbital billing patch notes\n2. zero trust notes\n"

R1_CLAIMS = [
    # every token of the claim appears in R1_TEXT -> lexical_overlap 1.0;
    # quote span is 30 chars ("ACME"+"SHIPS"+"ORBITAL"+"BILLING"+"2.0"+spaces)
    {"text": "Acme ships Orbital Billing 2.0.",
     "quote_span": "ACME SHIPS ORBITAL BILLING 2.0", "doc_id": "d1"},
    # "shipped" is not in the doc -> 3 of 4 claim tokens overlap = 0.75;
    # no quote span
    {"text": "risky behavior alerts shipped", "quote_span": None, "doc_id": "d1"},
]

ROWS = [
    {"doc_id": "d1", "text": R1_TEXT, "source": "blog", "claims": R1_CLAIMS},
    {"doc_id": "d2", "text": R2_TEXT, "source": "news_rss"},
    {"doc_id": "d3", "text": R3_TEXT, "source": "blog"},
]


def _profile(tmp_path: Path):
    """The served-problem vocabulary via the REAL loader — proves features.py
    consumes load_market_profile's shape instead of a hardcoded vocab."""
    cfg = tmp_path / "markets.yaml"
    cfg.write_text(
        "profiles:\n"
        "  lab:\n"
        "    seller: Test Seller\n"
        "    offerings:\n"
        "      - id: main\n"
        "        served_problem_phrases:\n"
        "          - security investigation\n"
        "          - risky behavior\n"
        "          - enterprise ai systems\n",
        encoding="utf-8",
    )
    return load_market_profile("lab", cfg)


# ------------------------------------------------- deterministic family (exact)


def test_deterministic_features_exact_on_hand_rows(tmp_path):
    matrix, names = features.deterministic_features(ROWS, _profile(tmp_path))

    assert names == [
        "char_count", "token_count", "line_count",
        "heading_density", "list_density",
        "profile_vocab_hits", "profile_tfidf_cosine",
        "lexical_overlap_mean", "lexical_overlap_max",
        "claim_count", "quote_span_mean_chars", "quote_span_frac",
        "source=blog", "source=news_rss",  # distinct sources, sorted
    ]
    assert matrix.shape == (3, len(names))

    # Vocabulary: N=3 docs; df(security investigation)=2, df(risky behavior)=1,
    # df(enterprise ai systems)=1. Smooth idf (sklearn's formula):
    #   idf = ln((1+N)/(1+df)) + 1; profile vector = idf (tf=1 per term).
    idf_si = math.log(4 / 3) + 1.0
    idf_rb = math.log(4 / 2) + 1.0
    idf_ea = math.log(4 / 2) + 1.0
    p = (idf_si, idf_rb, idf_ea)

    def _cos(doc_counts):
        d = tuple(c * w for c, w in zip(doc_counts, p))
        denom = math.sqrt(sum(x * x for x in d)) * math.sqrt(sum(x * x for x in p))
        return (sum(a * b for a, b in zip(d, p)) / denom) if denom else 0.0

    cos1 = _cos((1, 1, 0))  # R1 contains "security investigation", "risky behavior"
    cos2 = _cos((1, 0, 1))  # R2 contains "security investigation", "enterprise ai systems"
    cos3 = _cos((0, 0, 0))  # R3 contains none -> 0.0

    expected = [
        # R1: len(text) chars, len//4 tokens, 5 lines, 2/5 headings, 2/5 lists,
        # 2 vocab hits, 2 claims (overlaps 1.0 and 0.75), 30-char span mean,
        # 1 of 2 claims carries a span, source=blog.
        [float(len(R1_TEXT)), float(len(R1_TEXT) // 4), 5.0,
         2 / 5, 2 / 5, 2.0, cos1,
         (1.0 + 0.75) / 2, 1.0,
         2.0, 30.0, 1 / 2, 1.0, 0.0],
        # R2: no claims -> claim features all 0.
        [float(len(R2_TEXT)), float(len(R2_TEXT) // 4), 2.0,
         0.0, 0.0, 2.0, cos2,
         0.0, 0.0,
         0.0, 0.0, 0.0, 0.0, 1.0],
        # R3: 1/3 headings (the ALL-CAPS "CHANGELOG" line), 2/3 numbered lists.
        [float(len(R3_TEXT)), float(len(R3_TEXT) // 4), 3.0,
         1 / 3, 2 / 3, 0.0, cos3,
         0.0, 0.0,
         0.0, 0.0, 0.0, 1.0, 0.0],
    ]
    np.testing.assert_allclose(matrix, np.array(expected, dtype=float), atol=1e-9)


def test_deterministic_features_empty_profile_matches_nothing(tmp_path):
    # A profile with no offerings has an empty vocabulary: the market-profile
    # contract is that an unfilled profile matches NOTHING on purpose.
    from src.intel.market import MarketProfile

    empty = MarketProfile(profile_id="default", seller="", offerings=())
    matrix, names = features.deterministic_features(ROWS, empty)

    hits = names.index("profile_vocab_hits")
    cosine = names.index("profile_tfidf_cosine")
    assert names.index("source=blog") == len(names) - 2  # one-hots unaffected
    assert matrix[:, hits].tolist() == [0.0, 0.0, 0.0]
    assert matrix[:, cosine].tolist() == [0.0, 0.0, 0.0]
    # The structural columns still compute.
    assert matrix[0, names.index("claim_count")] == 2.0


def test_deterministic_features_missing_fields_are_zero():
    # Bare rows (no text/claims/source) must not raise: everything that has
    # no input is 0.0 — except token_count, which reuses gates.estimate_tokens
    # ("never zero" by that contract).
    matrix, names = features.deterministic_features([{}, {}], _empty_profile())
    assert names == list(features.DETERMINISTIC_NAMES)  # no source columns
    assert matrix.shape == (2, len(names))
    assert matrix[0, names.index("token_count")] == 1.0
    assert matrix[0, names.index("char_count")] == 0.0
    assert matrix[0, names.index("lexical_overlap_mean")] == 0.0
    assert not matrix.any(axis=None) or matrix[0, names.index("token_count")] == 1.0


def _empty_profile():
    from src.intel.market import MarketProfile

    return MarketProfile(profile_id="default", seller="", offerings=())


# ----------------------------------------------------- embedding family (fake)


class ArrayEmbedder:
    """Test double with the real Embedder contract: one deterministic
    384-dim unit vector per text (position = len(text) % DIM). Records
    every call so cache behavior is assertable."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def embed(self, texts):
        self.calls.append(list(texts))
        out = []
        for t in texts:
            v = np.zeros(DIM, dtype=np.float32)
            v[len(t) % DIM] = 1.0
            out.append(v.tolist())
        return out


def _expected_vec(text: str) -> np.ndarray:
    v = np.zeros(DIM, dtype=np.float32)
    v[len(text) % DIM] = 1.0
    return v


def test_embedding_features_embeds_dedupes_and_caches(tmp_path):
    cache = tmp_path / "embeddings"
    rows = [
        {"doc_id": "d1", "text": "alpha"},
        {"doc_id": "d2", "text": "beta beta"},
        {"doc_id": "d1", "text": "alpha"},  # duplicate doc: one embed, same vector
    ]
    emb = ArrayEmbedder()

    vectors, name = features.embedding_features(rows, embedder=emb, cache_dir=cache)

    assert name == "embedding"
    assert vectors.shape == (3, DIM)
    np.testing.assert_array_equal(vectors[0], _expected_vec("alpha"))
    np.testing.assert_array_equal(vectors[1], _expected_vec("beta beta"))
    np.testing.assert_array_equal(vectors[2], vectors[0])  # dup doc -> dup vector
    # Doc-level dedupe: the embedder saw each distinct doc exactly once.
    assert emb.calls == [["alpha", "beta beta"]]
    # Cache-forever files, one per doc_id.
    assert (cache / "d1.npy").exists() and (cache / "d2.npy").exists()


def test_embedding_features_served_from_cache_offline(tmp_path):
    cache = tmp_path / "embeddings"
    rows = [{"doc_id": "d1", "text": "alpha"}, {"doc_id": "d2", "text": "beta beta"}]

    first, _ = features.embedding_features(rows, embedder=ArrayEmbedder(), cache_dir=cache)
    # Once cached, the family is served with the embedder GONE (NullEmbedder):
    # offline forever after, never an error.
    second, name = features.embedding_features(rows, embedder=NullEmbedder(), cache_dir=cache)

    assert name == "embedding"
    np.testing.assert_array_equal(second, first)


def test_embedding_features_null_embedder_is_honest_none(tmp_path):
    cache = tmp_path / "embeddings"
    vectors, name = features.embedding_features(
        [{"doc_id": "d1", "text": "alpha"}], embedder=NullEmbedder(), cache_dir=cache
    )
    assert (vectors, name) == (None, "embedding")
    assert not cache.exists()  # nothing cached, no dir even created


def test_embedding_features_partial_cache_null_is_unavailable(tmp_path):
    # One doc cached, one not, embedder gone: the family must not silently
    # mix cached and missing vectors — honest None for the whole family.
    cache = tmp_path / "embeddings"
    features.embedding_features(
        [{"doc_id": "d1", "text": "alpha"}], embedder=ArrayEmbedder(), cache_dir=cache
    )
    vectors, name = features.embedding_features(
        [{"doc_id": "d1", "text": "alpha"}, {"doc_id": "d2", "text": "beta beta"}],
        embedder=NullEmbedder(),
        cache_dir=cache,
    )
    assert (vectors, name) == (None, "embedding")


def test_embedding_features_rows_without_doc_id_not_cached(tmp_path):
    cache = tmp_path / "embeddings"
    rows = [{"text": "no doc id here"}]
    emb = ArrayEmbedder()

    vectors, name = features.embedding_features(rows, embedder=emb, cache_dir=cache)
    assert name == "embedding"
    assert vectors.shape == (1, DIM)
    assert not list(cache.glob("*.npy")) if cache.exists() else True

    # Nothing cached -> the next call embeds again (and still works).
    again, _ = features.embedding_features(rows, embedder=emb, cache_dir=cache)
    assert again.shape == (1, DIM)
    assert len(emb.calls) == 2


def test_embedding_features_empty_rows_need_no_embedder():
    vectors, name = features.embedding_features([], embedder=NullEmbedder(), cache_dir="unused")
    assert name == "embedding"
    assert vectors.shape == (0, DIM)


# ------------------------------------------------------ the chronological split


def test_chronological_split_later_rows_in_test():
    rows = [
        {"generated_at": "2026-10-13T00:00:00+00:00"},  # latest
        {"generated_at": "2026-10-11T00:00:00+00:00"},
        {"generated_at": "2026-10-12T00:00:00+00:00"},
        {"generated_at": "2026-10-10T00:00:00+00:00"},  # earliest
    ]
    train, test = baselines.chronological_split(rows)
    # Train on the past, test on the future: Oct 10+11 train, Oct 12+13 test.
    assert sorted(train) == [1, 3]
    assert sorted(test) == [0, 2]


def test_chronological_split_falls_back_to_positional_halves():
    # No parseable timestamps anywhere: first half trains, second half tests,
    # in the GIVEN order (no sorting surprises).
    rows = [{"package": "pkg-d"}, {"package": "pkg-a"}, {"package": "pkg-c"}, {"package": "pkg-b"}]
    train, test = baselines.chronological_split(rows)
    assert train == [0, 1]
    assert test == [2, 3]


def test_chronological_split_from_package_timestamps_with_generated_at_precedence():
    # Package names carry the run timestamp (corpus rows: "d-<YYYYMMDDTHH...Z>-<run>");
    # an explicit generated_at outranks it when both exist.
    rows = [
        {"package": "acme.com-20261012T000000000000Z-aaa"},  # pkg ts: Oct 12
        {  # generated_at Oct 9 wins over the later package ts
            "generated_at": "2026-10-09T00:00:00+00:00",
            "package": "acme.com-20261013T000000000000Z-bbb",
        },
    ]
    train, test = baselines.chronological_split(rows)
    assert train == [1]  # earlier (Oct 9)
    assert test == [0]  # later (Oct 12)


# ------------------------------------------------------------ the baseline battery


def _synthetic_world():
    """24 rows, perfectly separable by the deterministic family's first
    column; timestamps strictly increase with row order."""
    n = 24
    signal = np.tile([-0.5, 0.5], n // 2)
    y = (signal > 0).astype(int)
    x_det = np.column_stack([signal, np.linspace(0.0, 1.0, n)])
    # Uninformative-but-valid embedding stand-in (bounded, no label signal).
    x_emb = np.column_stack([np.cos(np.linspace(0.0, 3.1, n)), np.zeros(n)])
    rows = [{"generated_at": f"2026-10-01T{i:02d}:00:00+00:00"} for i in range(n)]
    return x_det, x_emb, y, rows


def test_score_baselines_families_combined_and_mandate():
    pytest.importorskip("sklearn")
    x_det, x_emb, y, rows = _synthetic_world()

    result = baselines.score_baselines(
        {"deterministic": (x_det, y), "embedding": (x_emb, y)}, rows
    )

    # Per-family entries: {auroc, pr_auc, n} (+ the split sizes).
    det = result["deterministic"]
    assert det["n"] == 24 and det["n_train"] == 12 and det["n_test"] == 12
    assert det["auroc"] == pytest.approx(1.0)  # separable signal
    assert det["pr_auc"] > 0.99
    assert 0.0 <= result["embedding"]["auroc"] <= 1.0
    # Then combined: every family matrix stacked.
    assert result["combined"]["auroc"] == pytest.approx(1.0)
    assert result["combined"]["n"] == 24
    # Deployment-shaped split, named in the result (honest numbers name splits).
    assert result["split"] == "chronological"
    # The mandate line the whole plan hangs on.
    assert "discovered questions must beat the best single family" in result["mandate"]
    assert "deterministic" in result["mandate"]  # best single family named


def test_score_baselines_unavailable_family_is_honest():
    pytest.importorskip("sklearn")
    x_det, _x_emb, y, rows = _synthetic_world()

    result = baselines.score_baselines(
        {"deterministic": (x_det, y), "embedding": (None, y)}, rows
    )

    assert result["embedding"] == {
        "auroc": None, "pr_auc": None, "n": 24, "n_train": 12, "n_test": 12,
        "unavailable": True,
    }
    assert result["deterministic"]["auroc"] == pytest.approx(1.0)
    assert "combined" not in result  # one live matrix: nothing to combine
    assert "deterministic" in result["mandate"]


def test_score_baselines_single_class_test_split_is_honest_none():
    pytest.importorskip("sklearn")
    # Balanced train half, all-negative test half: AUROC is undefined on a
    # single-class test set — an honest None with a note, never a crash.
    y = np.array([0, 1] * 4 + [0] * 8)
    x = np.column_stack([np.arange(16, dtype=float)])
    result = baselines.score_baselines({"deterministic": (x, y)}, None)
    entry = result["deterministic"]
    assert entry["auroc"] is None and entry["pr_auc"] is None
    assert entry.get("note")


def test_score_baselines_mismatched_labels_is_an_error():
    pytest.importorskip("sklearn")
    x, _xe, y, rows = _synthetic_world()
    result = baselines.score_baselines(
        {"deterministic": (x, y), "embedding": (x, list(y[::-1]))}, rows
    )
    assert result.get("error", "").startswith("family")
    assert "labels differ" in result["error"]


def test_score_baselines_sklearn_missing_returns_honest_error(monkeypatch):
    def _boom():
        raise ImportError("No module named 'sklearn'")

    monkeypatch.setattr(baselines, "_import_stack", _boom)
    x_det, _x_emb, y, rows = _synthetic_world()
    assert baselines.score_baselines({"deterministic": (x_det, y)}, rows) == {
        "error": "sklearn unavailable"
    }
