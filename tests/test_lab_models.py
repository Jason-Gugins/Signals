"""Task 8 (decide-lab): model zoo, honest validation, plateau + stability.

The supervised layer and the discipline that keeps it honest:

* :mod:`src.lab.splits` — validation that matches deployment: expanding-
  window chronological folds (train is always STRICTLY earlier than test —
  deployment predicts future runs), GroupKFold-semantics domain folds (a
  domain never straddles train/test — no account leakage), and the final
  holdout: the most recent runs, returned once, never touched by discovery
  or model selection. Every fold carries its split label so every reported
  number can name the split that produced it.
* :mod:`src.lab.models.fit_compare` — elastic-net logistic, random forest,
  RBF SVR (a score model: labels 0/1, no predict_proba), histogram gradient
  boosting, and catboost when the optional extra exists (skipped honestly
  otherwise). AUROC + PR-AUC per fold with mean+/-std; probabilistic models
  additionally get isotonic/Platt recalibration with a Brier score before
  and after. Single-class folds report honest Nones, never crashes.
* :func:`src.lab.models.plateau` — the loop's stop rule (no mean-fold
  improvement >= epsilon for ``patience`` consecutive rounds).
* :func:`src.lab.models.stability_check` — a question/feature is stable
  when its importance-or-coefficient sign holds in >= ceil(2/3) of slices.

Synthetic worlds are deterministic (fixed RNG seeds); sklearn-needing tests
importorskip so the lane degrades gracefully without the ``signals[lab]``
extra, mirroring the rerank gating pattern.
"""
from __future__ import annotations

import sys

import pytest

np = pytest.importorskip("numpy")  # the whole lane rides the signals[lab] extra
pytest.importorskip("sklearn")

from src.lab import models, splits  # noqa: E402  (module tops stay stdlib-only)

# ------------------------------------------------------ deterministic worlds


def _ts(hour: int, day: int = 1) -> str:
    """A UTC stamp; lexicographic order == chronological order."""
    return f"2026-10-{day:02d}T{hour:02d}:00:00+00:00"


def _separable(n: int = 120, seed: int = 0, shift: float = 1.5):
    """Well-separated classes over 4 informative axes; timestamps strictly
    increase with row order and domains cycle, so every domain spans all
    of time (grouped folds stay honest on it)."""
    rng = np.random.default_rng(seed)
    y = rng.integers(0, 2, n)
    X = rng.normal(size=(n, 4)) + (y[:, None] * 2.0 - 1.0) * shift
    domains = [f"d{i % 3}" for i in range(n)]
    rows = [
        {"generated_at": _ts(i % 24, 1 + i // 24), "domain": domains[i]}
        for i in range(n)
    ]
    return X, y.astype(int), domains, rows


def _noisy(n: int = 180, seed: int = 7):
    """A Bayes-probabilistic world: latent z -> p = sigmoid(2z), y ~ Bern(p),
    features = z + heavy noise — label noise the models cannot see. Fully
    grown forests are overconfident here, so isotonic recalibration has
    real work to do (Brier before/after is meaningful)."""
    rng = np.random.default_rng(seed)
    z = rng.normal(size=n)
    p = 1.0 / (1.0 + np.exp(-2.0 * z))
    y = (rng.random(n) < p).astype(int)
    X = np.column_stack([z + rng.normal(scale=1.0, size=n), rng.normal(size=n)])
    domains = [f"d{i % 3}" for i in range(n)]
    rows = [
        {"generated_at": _ts(i % 24, 1 + i // 24), "domain": domains[i]}
        for i in range(n)
    ]
    return X, y, domains, rows


# ---------------------------------------------------------------- splits


def test_chronological_folds_never_train_on_the_future():
    # Shuffled row order, strictly increasing timestamps: every fold's train
    # block must sit STRICTLY earlier in time than its test block.
    rng = np.random.default_rng(3)
    order = list(rng.permutation(12))
    rows = [{"generated_at": _ts(i)} for i in order]
    folds = splits.chronological_folds(rows, 3)

    assert [f["label"] for f in folds] == ["chronological"] * 3
    assert [f["fold"] for f in folds] == [0, 1, 2]
    stamps = [_ts(i) for i in range(12)]
    row_stamp = [stamps[i] for i in order]
    for fold in folds:
        train, test = fold["train"], fold["test"]
        assert train and test
        assert set(train).isdisjoint(test)
        # The no-future-leakage invariant, checked on actual timestamps.
        assert max(row_stamp[i] for i in train) < min(row_stamp[i] for i in test)
    # Expanding window: the last fold trains on everything before its test.
    last = folds[-1]
    assert sorted(last["train"] + last["test"]) == list(range(12))
    # Test blocks advance in time and never overlap.
    assert set(folds[0]["test"]).isdisjoint(folds[1]["test"])
    assert (
        max(row_stamp[i] for i in folds[0]["test"])
        < min(row_stamp[i] for i in folds[1]["test"])
    )


def test_chronological_folds_validations():
    with pytest.raises(ValueError):  # n < n_splits + 1
        splits.chronological_folds([{"generated_at": _ts(0)}], 1)
    with pytest.raises(ValueError):
        splits.chronological_folds([], 1)
    with pytest.raises(ValueError):  # n_splits < 1
        splits.chronological_folds([{} for _ in range(10)], 0)


def test_grouped_folds_never_split_a_domain():
    rows = (
        [{"domain": "a"} for _ in range(5)]
        + [{"domain": "b"} for _ in range(4)]
        + [{"domain": "c"} for _ in range(3)]
    )
    folds = splits.grouped_folds(rows, 3)

    assert [f["label"] for f in folds] == ["grouped"] * 3
    seen: set[int] = set()
    for fold in folds:
        train, test = fold["train"], fold["test"]
        assert train and test
        assert set(train).isdisjoint(test)
        # The no-leakage invariant: a domain lives wholly in train or test.
        doms_train = {rows[i]["domain"] for i in train}
        doms_test = {rows[i]["domain"] for i in test}
        assert doms_train.isdisjoint(doms_test)
        assert set(test).isdisjoint(seen)  # every domain tests exactly once
        seen |= set(test)
    assert seen == set(range(12))  # every row is tested exactly once


def test_grouped_folds_need_enough_domains():
    with pytest.raises(ValueError):  # fewer domains than folds
        splits.grouped_folds([{"domain": "a"}, {"domain": "b"}], 3)
    with pytest.raises(ValueError):  # GroupKFold semantics need >= 2 folds
        splits.grouped_folds([{"domain": "a"}] * 4, 1)


def test_final_holdout_is_the_most_recent_and_disjoint():
    rng = np.random.default_rng(5)
    order = list(rng.permutation(10))
    rows = [{"generated_at": _ts(i)} for i in order]
    hold = splits.final_holdout(rows, frac=0.2)

    assert hold["label"] == "holdout"
    train, test = hold["train"], hold["test"]
    assert len(test) == 2  # 20% of 10
    assert set(train).isdisjoint(test)
    assert sorted(train + test) == list(range(10))
    stamps = [_ts(i) for i in range(10)]
    row_stamp = [stamps[i] for i in order]
    # Untouched by discovery AND selection means: it is the newest data,
    # carved off once, and everything earlier is the working set.
    assert max(row_stamp[i] for i in train) < min(row_stamp[i] for i in test)
    latest = sorted(range(10), key=lambda i: row_stamp[i])[-2:]
    assert sorted(test) == sorted(latest)


def test_final_holdout_validations():
    with pytest.raises(ValueError):
        splits.final_holdout([{"generated_at": _ts(0)}], frac=0.0)
    with pytest.raises(ValueError):
        splits.final_holdout([{"generated_at": _ts(0)}], frac=1.0)
    with pytest.raises(ValueError):  # nothing to carve off
        splits.final_holdout([{"generated_at": _ts(0)}], frac=0.2)


# ------------------------------------------------------------- fit_compare


def test_fit_compare_every_model_beats_chance_chronological():
    X, y, _domains, rows = _separable()
    folds = splits.chronological_folds(rows, 3)

    result = models.fit_compare(X, y, None, folds)

    assert result["split"] == "chronological"
    assert result["n_folds"] == 3
    assert result["n"] == 120
    assert 0.0 < result["positive_rate"] < 1.0  # class balance disclosed
    for name in (
        "elastic_net_logistic",
        "random_forest",
        "svr_rbf",
        "hist_gradient_boosting",
    ):
        entry = result["models"][name]
        assert entry["auroc_mean"] > 0.75, name  # chance is 0.5
        assert entry["pr_auc_mean"] > 0.75, name
        assert entry["auroc_std"] >= 0.0
        assert entry["n_folds_scored"] == 3
        assert len(entry["per_fold"]) == 3
        for pf in entry["per_fold"]:
            assert pf["auroc"] is not None and pf["pr_auc"] is not None


def test_fit_compare_grouped_folds_and_probabilistic_contract():
    X, y, domains, rows = _separable()
    folds = splits.grouped_folds(rows, 3)

    result = models.fit_compare(X, y, domains, folds)

    assert result["split"] == "grouped"
    for name in (
        "elastic_net_logistic",
        "random_forest",
        "svr_rbf",
        "hist_gradient_boosting",
    ):
        assert result["models"][name]["auroc_mean"] > 0.75, name
    # Probabilistic models carry a Brier before AND after recalibration;
    # the SVR is a score model (no predict_proba) and says so honestly.
    rf = result["models"]["random_forest"]
    assert rf["probabilistic"] is True
    assert rf["brier_raw_mean"] is not None
    assert rf["brier_calibrated_mean"] is not None
    svr = result["models"]["svr_rbf"]
    assert svr["probabilistic"] is False
    assert svr["brier_raw_mean"] is None
    assert svr["brier_calibrated_mean"] is None


def test_fit_compare_single_class_fold_is_honest_none():
    n = 24
    rng = np.random.default_rng(11)
    y = np.array([0, 1] * 8 + [0] * 8)  # the LAST time block is all-negative
    X = rng.normal(size=(n, 3)) + (y[:, None] * 2.0 - 1.0) * 1.5
    rows = [{"generated_at": _ts(i)} for i in range(n)]
    folds = splits.chronological_folds(rows, 2)

    result = models.fit_compare(X, y, None, folds)
    entry = result["models"]["elastic_net_logistic"]
    assert entry["per_fold"][1]["auroc"] is None
    assert entry["per_fold"][1]["pr_auc"] is None
    assert "single-class" in entry["per_fold"][1]["note"]
    # Means come from the folds that could score — never a fabricated value.
    assert entry["auroc_mean"] == pytest.approx(entry["per_fold"][0]["auroc"])
    assert entry["auroc_std"] is not None
    assert entry["n_folds_scored"] == 1


def test_fit_compare_accepts_a_bare_holdout_fold():
    X, y, _domains, rows = _separable(n=60)
    hold = splits.final_holdout(rows, frac=0.2)

    result = models.fit_compare(X, y, None, hold)  # a single fold dict

    assert result["split"] == "holdout"
    assert result["n_folds"] == 1
    assert result["models"]["hist_gradient_boosting"]["auroc_mean"] > 0.75


def test_fit_compare_input_honesty():
    X, y, _domains, rows = _separable(n=30)
    folds = splits.chronological_folds(rows, 2)
    assert models.fit_compare(X, list(y)[:-1] + [2], None, folds).get("error")
    assert models.fit_compare(X, y[:-1], None, folds).get("error")
    bad = [{"label": "chronological", "train": [0], "test": [99]}]
    assert models.fit_compare(X, y, None, bad).get("error")  # index out of range
    assert models.fit_compare(X, y, None, []).get("error")  # no folds
    bad_groups = ["only-one-group"] * 30
    assert models.fit_compare(X, y, bad_groups[:-1], folds).get("error")


# ------------------------------------------------------------- recalibration


def test_recalibration_improves_overconfident_random_forest():
    X, y, domains, rows = _noisy()
    folds = splits.grouped_folds(rows, 3)

    result = models.fit_compare(X, y, domains, folds)

    rf = result["models"]["random_forest"]
    # The fully grown forest is overconfident on the noisy world: isotonic
    # recalibration must strictly improve it (and never regress it).
    assert rf["brier_calibrated_mean"] <= rf["brier_raw_mean"] + 1e-12
    assert rf["brier_calibrated_mean"] < rf["brier_raw_mean"]
    for pf in rf["per_fold"]:
        assert pf["calibration"] in {"isotonic", "sigmoid"}
        assert pf["brier_raw"] is not None
        assert pf["brier_calibrated"] is not None
    # Logistic is near-calibrated by construction: recalibration is at worst
    # a rounding-level change (the no-regression half of the contract).
    lr = result["models"]["elastic_net_logistic"]
    assert lr["brier_calibrated_mean"] <= lr["brier_raw_mean"] + 0.01


def test_recalibration_platt_fallback_when_isotonic_unavailable(monkeypatch):
    # Platt (sigmoid) where isotone is unavailable: force the fallback and
    # require it still produces a calibrated Brier on every fold.
    monkeypatch.setattr(models, "_calibration_methods", lambda stack: ("sigmoid",))
    X, y, _domains, rows = _separable(n=60)
    folds = splits.chronological_folds(rows, 2)

    result = models.fit_compare(X, y, None, folds)

    for name in ("elastic_net_logistic", "random_forest", "hist_gradient_boosting"):
        for pf in result["models"][name]["per_fold"]:
            assert pf["calibration"] == "sigmoid", name
            assert pf["brier_calibrated"] is not None


# ------------------------------------------------------------------ catboost


def test_catboost_skipped_honestly_when_absent(monkeypatch):
    # Simulate the import being unavailable even where catboost IS installed:
    # a None module in sys.modules halts the import machinery for that name.
    monkeypatch.setitem(sys.modules, "catboost", None)
    X, y, _domains, rows = _separable(n=60)

    result = models.fit_compare(X, y, None, splits.chronological_folds(rows, 2))

    cb = result["models"]["catboost"]
    assert cb.get("skipped") is True
    assert cb.get("auroc_mean") is None
    assert "catboost" in cb.get("note", "").lower()
    # The rest of the zoo is unaffected by the skip.
    assert result["models"]["random_forest"]["auroc_mean"] > 0.75


# ----------------------------------------------------- sklearn-missing honesty


def test_fit_compare_sklearn_missing_is_honest_error(monkeypatch):
    def _boom():
        raise ImportError("No module named 'sklearn'")

    monkeypatch.setattr(models, "_import_stack", _boom)
    X, y, _domains, rows = _separable(n=30)
    result = models.fit_compare(X, y, None, splits.chronological_folds(rows, 2))
    assert result == {"error": "sklearn unavailable"}


# ------------------------------------------------------------------- plateau


def test_plateau_fires_after_patience():
    assert models.plateau([]) is False
    assert models.plateau([0.5]) is False  # the first round only baselines
    assert models.plateau([0.5, 0.52]) is False  # improved >= epsilon
    assert models.plateau([0.5, 0.501, 0.502]) is True  # two flat rounds
    assert models.plateau([0.5, 0.4, 0.3]) is True  # monotone decay
    assert models.plateau([0.5, 0.501, 0.52]) is False  # late recovery
    assert models.plateau([0.5, 0.52, 0.530]) is False  # 0.010 >= 0.005
    # patience=3 needs THREE consecutive flat rounds.
    assert models.plateau([0.5, 0.501, 0.502, 0.503], patience=3) is True
    assert models.plateau([0.5, 0.501, 0.52, 0.526], patience=3) is False
    # epsilon is the improvement bar.
    assert models.plateau([0.5, 0.52, 0.54], epsilon=0.03) is True
    assert models.plateau([0.5, 0.52, 0.54], epsilon=0.01) is False
    # A round with no score (None) never counts as an improvement.
    assert models.plateau([0.5, None, 0.501, 0.502]) is True


# ----------------------------------------------------------------- stability


def test_stability_check_keeps_two_of_three():
    # Three domain slices of per-question signed importances/coefficients.
    # q_strong holds its (positive) sign everywhere; q_flippy holds it in 2
    # of 3 (one dissent); q_shy appears with mixed signs and one absence —
    # no sign holds in 2/3, so it does not survive.
    runs_by_slice = {
        "enterprise": {"q_strong": 0.8, "q_flippy": 0.3, "q_shy": 0.2, "q_neg": -0.7},
        "midmarket": {"q_strong": 0.7, "q_flippy": -0.4, "q_shy": -0.1, "q_neg": -0.6},
        "smb": {"q_strong": 0.9, "q_flippy": 0.1, "q_neg": -0.8},
    }

    out = models.stability_check(runs_by_slice)

    assert out["n_slices"] == 3
    assert out["threshold"] == 2  # ceil(2/3 * 3)
    assert set(out["stable"]) == {"q_strong", "q_flippy", "q_neg"}
    assert set(out["unstable"]) == {"q_shy"}
    assert out["unstable"]["q_shy"] == {
        "positive": 1, "negative": 1, "zero": 0, "absent": 1,
    }


def test_stability_check_threshold_semantics():
    runs_by_slice = {
        "a": {"q1": 0.5, "q2": 0.4},
        "b": {"q1": 0.6, "q2": -0.4},
        "c": {"q1": 0.7, "q2": -0.3},
    }
    # A stricter bar (all slices): only the unanimous q1 survives.
    strict = models.stability_check(runs_by_slice, min_slices_useful=1.0)
    assert strict["threshold"] == 3
    assert strict["stable"] == ["q1"]
    # An absolute count works too.
    absolute = models.stability_check(runs_by_slice, min_slices_useful=3)
    assert absolute["stable"] == ["q1"]
    # Two slices: ceil(2/3 * 2) = 2 — both must agree.
    two = models.stability_check({k: v for k, v in list(runs_by_slice.items())[:2]})
    assert two["threshold"] == 2
    assert two["stable"] == ["q1"]
    # Degenerate worlds are honest, never errors.
    empty = models.stability_check({})
    assert empty == {"n_slices": 0, "threshold": 0, "stable": [], "unstable": {}}
    with pytest.raises(ValueError):
        models.stability_check(runs_by_slice, min_slices_useful=0)
