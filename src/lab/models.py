"""Model zoo + the honesty layer (decide-lab Task 8).

:func:`fit_compare` scores four model families over a caller-supplied fold
set (from :mod:`src.lab.splits` — every reported number names the split
that produced it): elastic-net logistic regression, random forest, RBF SVR
(a SCORE model: fit on the 0/1 labels, evaluated by the ranking metrics on
its continuous output — it has no probabilities to calibrate), and
histogram gradient boosting. CatBoost joins the zoo only when the optional
extra is importable; when it is not — it is deliberately NOT part of
``signals[lab]`` — its entry says so and the comparison proceeds (an
honest skip, never an error).

Per fold, every model gets AUROC + PR-AUC; probabilistic models (those
exposing ``predict_proba``) additionally get recalibration — isotonic via
sklearn's :class:`~sklearn.calibration.CalibratedClassifierCV`, Platt
(sigmoid) where isotone is unavailable or fails on the fold — with a Brier
score before and after. Calibration is fit on the fold's TRAIN side only,
under an inner cross-validation that respects ``groups`` (GroupKFold) when
domains are supplied, so recalibration never leaks across folds or
domains. Means/stds are taken across the folds that could score; a
single-class or degenerate fold reports honest Nones with a note, and
never crashes the comparison.

Loop discipline lives here too:

* :func:`plateau` — the stop rule: fire when no round improved on the
  best mean-fold score by >= epsilon for ``patience`` consecutive rounds.
* :func:`stability_check` — a question/feature is stable when its
  importance-or-coefficient SIGN holds in >= ceil(min_slices_useful * n)
  slices (default 2/3 of them, rounded up).

Module top is stdlib-only (the ``signals[lab]`` extra is imported lazily
via :func:`_import_stack`; without it :func:`fit_compare` returns an
honest ``{"error": "sklearn unavailable"}`` — the same contract as
:mod:`src.lab.baselines`). :func:`plateau` and :func:`stability_check`
stay pure stdlib so the round runner can consult them anywhere.
"""

from __future__ import annotations

import math
from types import SimpleNamespace
from typing import Any, Callable

ERROR_SKLEARN_UNAVAILABLE = "sklearn unavailable"

MODEL_ELASTIC_NET = "elastic_net_logistic"
MODEL_RANDOM_FOREST = "random_forest"
MODEL_SVR_RBF = "svr_rbf"
MODEL_HIST_GB = "hist_gradient_boosting"
MODEL_CATBOOST = "catboost"

#: The per-fold metrics that aggregate into mean/std entries.
SCORING_METRICS = ("auroc", "pr_auc", "brier_raw", "brier_calibrated")


def _import_stack() -> SimpleNamespace:
    """The numpy/sklearn pieces the zoo needs, or ImportError — the honest
    gate for environments without the ``signals[lab]`` extra."""
    import numpy as np
    from sklearn import __version__ as sklearn_version
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
    from sklearn.model_selection import GroupKFold, KFold
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.svm import SVR

    try:  # isotonic is part of core sklearn, but the fallback stays honest
        from sklearn.isotonic import IsotonicRegression  # noqa: F401

        isotonic_ok = True
    except Exception:
        isotonic_ok = False

    return SimpleNamespace(
        np=np,
        sklearn_version=tuple(int(p) for p in sklearn_version.split(".")[:2]),
        isotonic_ok=isotonic_ok,
        CalibratedClassifierCV=CalibratedClassifierCV,
        GroupKFold=GroupKFold,
        HistGradientBoostingClassifier=HistGradientBoostingClassifier,
        KFold=KFold,
        LogisticRegression=LogisticRegression,
        Pipeline=Pipeline,
        RandomForestClassifier=RandomForestClassifier,
        SVR=SVR,
        StandardScaler=StandardScaler,
        average_precision_score=average_precision_score,
        brier_score_loss=brier_score_loss,
        roc_auc_score=roc_auc_score,
    )


def _import_catboost() -> Any | None:
    """catboost's classifier, or None when the optional extra is absent
    (it is NOT part of ``signals[lab]`` — the cookbook-faithful comparison
    includes it only where someone installed it by hand)."""
    try:
        from catboost import CatBoostClassifier
    except Exception:
        return None
    return CatBoostClassifier


def _calibration_methods(stack: SimpleNamespace) -> tuple[str, ...]:
    """Recalibration ladder: isotonic first, Platt (sigmoid) where isotone
    is unavailable. Kept as its own function so tests can force the Platt
    path."""
    if getattr(stack, "isotonic_ok", False):
        return ("isotonic", "sigmoid")
    return ("sigmoid",)


def _elastic_net(stack: SimpleNamespace) -> Any:
    """Elastic-net logistic (l1_ratio=0.5), constructed version-portably
    (mirrors :mod:`src.lab.baselines`): sklearn >= 1.8 deprecates
    ``penalty="elasticnet"`` in favor of ``l1_ratio`` alone, while older
    sklearn ignores ``l1_ratio`` unless the penalty is explicit."""
    common = {"solver": "saga", "l1_ratio": 0.5, "C": 1.0, "max_iter": 5000, "random_state": 0}
    if stack.sklearn_version >= (1, 8):
        return stack.LogisticRegression(**common)
    return stack.LogisticRegression(penalty="elasticnet", **common)


def _model_specs(stack: SimpleNamespace) -> list[tuple[str, Callable[[], Any], bool]]:
    """(name, fresh-estimator builder, probabilistic) per zoo model. A NEW
    estimator is built per fold (and per calibration) — nothing carries a
    fit across boundaries. Fixed random_state/random_seed everywhere: the
    comparison is deterministic."""
    return [
        (
            MODEL_ELASTIC_NET,
            lambda: stack.Pipeline(
                [("scale", stack.StandardScaler()), ("lr", _elastic_net(stack))]
            ),
            True,
        ),
        (MODEL_RANDOM_FOREST, lambda: stack.RandomForestClassifier(
            n_estimators=200, random_state=0
        ), True),
        (
            MODEL_SVR_RBF,
            lambda: stack.Pipeline(
                [("scale", stack.StandardScaler()), ("svr", stack.SVR(kernel="rbf"))]
            ),
            False,
        ),
        (MODEL_HIST_GB, lambda: stack.HistGradientBoostingClassifier(random_state=0), True),
    ]


def _inner_cv(stack: SimpleNamespace, X: Any, y: Any, groups: Any, n_splits: int = 3):
    """Leak-free inner CV for recalibration, over the fold's TRAIN side.

    With domains supplied the inner splits are GroupKFold (recalibration
    never mixes a domain across its inner train/validation); otherwise a
    seeded shuffled KFold. Returns a precomputed list of splits — handing
    CalibratedClassifierCV a list sidesteps its lack of a ``groups``
    parameter. None when the train side cannot support >= 2 inner splits.
    """
    if groups is not None:
        n_groups = len(set(groups.tolist()))
        k = min(n_splits, n_groups)
        if k >= 2:
            splitter = stack.GroupKFold(n_splits=k)
            return list(splitter.split(X, y, groups))
    k = min(n_splits, len(X))
    if k < 2:
        return None
    splitter = stack.KFold(n_splits=k, shuffle=True, random_state=0)
    return list(splitter.split(X, y))


def _calibrate_fold(
    stack: SimpleNamespace,
    np: Any,
    out: dict,
    builder: Callable[[], Any],
    X: Any,
    y: Any,
    groups: Any,
    train_idx: list[int],
    test_idx: list[int],
) -> None:
    """Recalibrate on the fold's train side and Brier the calibrated
    probabilities on the untouched test side; fills ``calibration`` /
    ``brier_calibrated`` (or an honest note)."""
    X_train, y_train = X[train_idx], y[train_idx]
    groups_train = None if groups is None else groups[train_idx]
    inner = _inner_cv(stack, X_train, y_train, groups_train)
    if inner is None:
        out["calibration_note"] = "train side too small to recalibrate"
        return
    for method in _calibration_methods(stack):
        try:
            calibrated = stack.CalibratedClassifierCV(
                estimator=builder(), method=method, cv=inner
            )
            calibrated.fit(X_train, y_train)
            proba = np.asarray(calibrated.predict_proba(X[test_idx])[:, 1], dtype=float)
        except Exception:
            continue  # isotonic can genuinely fail on tiny/awkward folds -> Platt
        out["calibration"] = method
        out["brier_calibrated"] = float(stack.brier_score_loss(y[test_idx], proba))
        return
    out["calibration_note"] = "recalibration failed (isotonic and Platt)"


def _score_fold(
    stack: SimpleNamespace,
    np: Any,
    builder: Callable[[], Any],
    probabilistic: bool,
    X: Any,
    y: Any,
    groups: Any,
    train_idx: list[int],
    test_idx: list[int],
) -> dict:
    """Fit one fresh estimator on the fold's train side and score its test
    side. Undefined metrics (single-class splits, failed fits) are honest
    Nones with a note — a fold-level fact, never a crash."""
    out: dict[str, Any] = {
        "auroc": None,
        "pr_auc": None,
        "brier_raw": None,
        "brier_calibrated": None,
        "calibration": None,
        "n_train": len(train_idx),
        "n_test": len(test_idx),
    }
    if len(train_idx) < 2 or len(test_idx) < 1:
        out["note"] = "split too small"
        return out
    y_train, y_test = y[train_idx], y[test_idx]
    if len(set(y_train.tolist())) < 2:
        out["note"] = "single-class train split"
        return out
    try:
        model = builder()
        model.fit(X[train_idx], y_train)
    except Exception as exc:
        out["note"] = f"fit failed: {exc}"
        return out

    if probabilistic and hasattr(model, "predict_proba"):
        scores = np.asarray(model.predict_proba(X[test_idx])[:, 1], dtype=float)
        out["brier_raw"] = float(stack.brier_score_loss(y_test, scores))
        # Brier is defined on any label mix, so recalibrate even when the
        # ranking metrics below will be undefined on this test side.
        _calibrate_fold(stack, np, out, builder, X, y, groups, train_idx, test_idx)
    else:
        scores = np.asarray(model.predict(X[test_idx]), dtype=float).ravel()

    if len(set(y_test.tolist())) < 2:
        out["note"] = "metrics undefined: single-class test split"
        return out
    try:
        auroc = float(stack.roc_auc_score(y_test, scores))
        pr_auc = float(stack.average_precision_score(y_test, scores))
    except ValueError as exc:  # newer sklearn can raise on degenerate folds
        out.setdefault("note", f"metrics undefined: {exc}")
        return out
    if math.isnan(auroc) or math.isnan(pr_auc):  # or warn-and-NaN
        out.setdefault("note", "metrics undefined: degenerate test split")
        return out
    out["auroc"], out["pr_auc"] = auroc, pr_auc
    return out


def _agg(np: Any, values: list[Any]) -> tuple[float | None, float | None]:
    """(mean, population std) over the non-None values; (None, None) when
    nothing could score — the mean is never fabricated."""
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return None, None
    return float(np.mean(vals)), float(np.std(vals))


def _score_model(
    stack: SimpleNamespace,
    np: Any,
    builder: Callable[[], Any],
    probabilistic: bool,
    X: Any,
    y: Any,
    groups: Any,
    fold_payload: list[tuple[str, list[int], list[int]]],
) -> dict:
    """One zoo model across every fold: per-fold entries plus mean/std per
    metric and the count of folds that actually scored."""
    per_fold = [
        _score_fold(stack, np, builder, probabilistic, X, y, groups, tr, te)
        for _label, tr, te in fold_payload
    ]
    entry: dict[str, Any] = {"probabilistic": probabilistic, "per_fold": per_fold}
    for metric in SCORING_METRICS:
        mean, std = _agg(np, [pf.get(metric) for pf in per_fold])
        entry[f"{metric}_mean"] = mean
        entry[f"{metric}_std"] = std
    entry["n_folds_scored"] = sum(1 for pf in per_fold if pf.get("auroc") is not None)
    return entry


def fit_compare(feature_matrix: Any, labels: Any, groups: Any, folds: Any) -> dict:
    """Compare the model zoo over a fold set — the supervised layer.

    ``feature_matrix`` is the row-aligned numeric matrix (the encoded
    noul/question columns and/or the Task 4 families), ``labels`` the 0/1
    targets, ``groups`` the row-aligned domain labels (or None) used by
    the inner recalibration CV, and ``folds`` the output of
    :func:`src.lab.splits.chronological_folds` /
    :func:`src.lab.splits.grouped_folds` — or a bare fold dict such as
    :func:`src.lab.splits.final_holdout`'s. The result names its split
    (``"split"``), discloses the class balance (``positive_rate``), and
    carries per-model ``per_fold`` entries plus ``auroc``/``pr_auc``/
    ``brier_raw``/``brier_calibrated`` mean/std across folds.

    Degenerate inputs return honest ``{"error": ...}`` dicts — most
    importantly ``{"error": "sklearn unavailable"}`` without the lab
    extra. CatBoost appears as ``{"skipped": True, ...}`` when the
    optional extra is not installed.
    """
    try:
        stack = _import_stack()
    except ImportError:
        return {"error": ERROR_SKLEARN_UNAVAILABLE}
    np = stack.np

    X = np.asarray(feature_matrix, dtype=float)
    y = np.asarray(labels, dtype=int).ravel()
    if X.ndim != 2 or X.shape[1] == 0:
        return {"error": "feature_matrix must be 2-D with at least one column"}
    if y.shape[0] != X.shape[0]:
        return {"error": "labels and feature_matrix row counts differ"}
    if not set(np.unique(y).tolist()) <= {0, 1}:
        return {"error": "labels must be 0/1"}
    group_labels: Any = None
    if groups is not None:
        group_labels = np.asarray(groups).ravel()
        if group_labels.shape[0] != X.shape[0]:
            return {"error": "groups and feature_matrix row counts differ"}

    if isinstance(folds, dict):
        folds = [folds]
    fold_payload: list[tuple[str, list[int], list[int]]] = []
    for fold in folds:
        if not isinstance(fold, dict) or "train" not in fold or "test" not in fold:
            return {"error": "each fold needs train/test index lists"}
        train_idx = [int(i) for i in fold["train"]]
        test_idx = [int(i) for i in fold["test"]]
        if any(i < 0 or i >= X.shape[0] for i in (*train_idx, *test_idx)):
            return {"error": "fold indices out of range"}
        fold_payload.append((str(fold.get("label") or "unlabeled"), train_idx, test_idx))
    if not fold_payload:
        return {"error": "no folds"}

    result: dict[str, Any] = {
        "split": "+".join(sorted({label for label, _tr, _te in fold_payload})),
        "n_folds": len(fold_payload),
        "n": int(X.shape[0]),
        "n_features": int(X.shape[1]),
        "positive_rate": float(y.mean()),
        "models": {},
    }

    specs = _model_specs(stack)
    catboost_cls = _import_catboost()
    if catboost_cls is None:
        result["models"][MODEL_CATBOOST] = {
            "skipped": True,
            "probabilistic": True,
            "auroc_mean": None,
            "note": (
                "catboost not installed — optional extra, not part of "
                "signals[lab]; skipped honestly"
            ),
        }
    else:
        specs.append(
            (
                MODEL_CATBOOST,
                lambda: catboost_cls(iterations=200, verbose=0, random_seed=0),
                True,
            )
        )
    for name, builder, probabilistic in specs:
        result["models"][name] = _score_model(
            stack, np, builder, probabilistic, X, y, group_labels, fold_payload
        )
    return result


# ------------------------------------------------------------- loop control


def plateau(history: Any, *, epsilon: float = 0.005, patience: int = 2) -> bool:
    """The stop rule: True when the loop has plateaued.

    ``history`` is the per-round mean-fold score sequence. A round
    IMPROVES when it beats the best score so far by >= ``epsilon`` — and
    the best ALWAYS rises to the maximum observed, even when that gain was
    sub-epsilon, so a steady drip of small gains is honestly a plateau
    (each round's improvement is measured per round, never cumulatively
    against a stale baseline). The first numeric round only sets the
    baseline and is never itself an improvement; a dip-and-recover does
    not reset the standard. Plateau fires when ``patience`` consecutive
    rounds failed to improve. Rounds without a score (None) are skipped
    until the first numeric baseline exists, and afterwards count as
    non-improving — a round that produced no number did not get better.
    """
    if patience < 1:
        raise ValueError("patience must be >= 1")
    best: float | None = None
    streak = 0
    for score in history:
        if best is None:
            if isinstance(score, (int, float)):
                best = float(score)
            continue
        numeric = isinstance(score, (int, float))
        if numeric and (score - best) >= epsilon:
            streak = 0
        else:
            streak += 1
            if streak >= patience:
                return True
        if numeric:
            best = max(best, float(score))  # the bar rises to the best seen
    return False


def stability_check(
    runs_by_slice: dict[str, dict[str, Any]], *, min_slices_useful: float = 2 / 3
) -> dict:
    """Cross-slice stability of questions/features — the keep rule.

    ``runs_by_slice`` maps slice name (the 3 domain slices the discovery
    loop reran over) to that slice's per-question signed value (a feature
    importance or regression coefficient — the SIGN is what must hold). A
    question is STABLE when one sign is carried in >=
    ``ceil(min_slices_useful * n_slices)`` slices (default 2/3 of them,
    rounded up); slices where the question is absent, null, or exactly
    zero give it no vote, and the denominator is every slice.

    ``min_slices_useful`` at or below 1 is a share of slices (the default
    2/3; 1.0 means "every slice"); values above 1 are absolute slice
    counts. Returns
    ``{"n_slices", "threshold", "stable": [names], "unstable":
    {name: {"positive", "negative", "zero", "absent"}}}``.
    """
    if min_slices_useful <= 0:
        raise ValueError("min_slices_useful must be positive")
    n = len(runs_by_slice)
    if min_slices_useful <= 1:  # a share of the slices, rounded up (1.0 = all)
        threshold = math.ceil(min_slices_useful * n)
    else:  # an absolute count
        threshold = int(math.ceil(min_slices_useful))
    threshold = min(threshold, n)

    names: set[str] = set()
    for run in runs_by_slice.values():
        if isinstance(run, dict):
            names.update(str(key) for key in run)
    tally = {name: {"positive": 0, "negative": 0, "zero": 0, "absent": 0} for name in names}
    for run in runs_by_slice.values():
        for name in names:
            raw = run.get(name) if isinstance(run, dict) else None
            try:
                value = float(raw)
            except (TypeError, ValueError):
                value = None
            if value is None:
                tally[name]["absent"] += 1
            elif value > 0:
                tally[name]["positive"] += 1
            elif value < 0:
                tally[name]["negative"] += 1
            else:
                tally[name]["zero"] += 1

    stable: list[str] = []
    unstable: dict[str, dict[str, int]] = {}
    for name in sorted(tally):
        counts = tally[name]
        held = max(counts["positive"], counts["negative"])
        if held >= threshold:
            stable.append(name)
        else:
            unstable[name] = counts
    return {"n_slices": n, "threshold": threshold, "stable": stable, "unstable": unstable}
