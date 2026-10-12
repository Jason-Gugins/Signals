"""Baseline battery (decide-lab Task 4) — the bar every discovered gate
question must beat.

:func:`score_baselines` fits an elastic-net logistic regression on each
feature family separately (:mod:`src.lab.features`), then on the combined
stack, and scores AUROC + PR-AUC on a CHRONOLOGICAL split: rows are sorted
by their timestamps (``generated_at`` first, then the package-name run
timestamp, then ``fetched_at``) and the LATER half is the test set —
deployment predicts future runs, so validation must too. When no timestamps
parse, the split falls back to positional first-half/second-half of the
given row order.

The result dict carries ``{family: {auroc, pr_auc, n, ...}}``, a
``combined`` entry, the split label (every reported number names its
split), and the ``mandate`` line the whole plan hangs on:
:data:`MANDATE` — discovered questions must beat the best single family
(the "combined" stack is not a single family and never wins the mandate).

sklearn/numpy are imported lazily via :func:`_import_stack` — without the
``signals[lab]`` extra the scorer returns an honest
``{"error": "sklearn unavailable"}`` instead of raising, mirroring the
rerank gating pattern. Module top stays stdlib-only so the module imports
anywhere.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

#: The report line every autoresearch round is judged against.
MANDATE = "discovered questions must beat the best single family."

SPLIT_CHRONOLOGICAL = "chronological"
SPLIT_FALLBACK = "first-half/second-half (no timestamps)"

# "<domain>-<YYYYMMDDTHHMMSS(+up to 6 more digits)>Z-<run_id>" — the corpus
# package-name run timestamp (corpus.py anchors domain filtering on the
# same shape).
_PKG_TS_RE = re.compile(r"-(\d{8}T\d{6,14}Z?)-")


def _parse_iso(value: object) -> float | None:
    try:
        s = str(value).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _parse_pkg_ts(package: object) -> float | None:
    match = _PKG_TS_RE.search(str(package or ""))
    if not match:
        return None
    body = match.group(1)
    if body.endswith("Z"):
        body = body[:-1]
    try:
        dt = datetime.strptime(body[:14], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        frac = body[14:]
        if frac:
            dt = dt.replace(microsecond=int(frac[:6].ljust(6, "0")))
    except ValueError:
        return None
    return dt.timestamp()


def row_time(row: dict) -> float | None:
    """The row's sortable timestamp (epoch seconds) or None.

    Precedence: ``generated_at`` (the manifest's run time), the package
    name's embedded run timestamp, then the doc's ``fetched_at``.
    """
    for parser, key in (
        (_parse_iso, "generated_at"),
        (_parse_pkg_ts, "package"),
        (_parse_iso, "fetched_at"),
    ):
        if row.get(key):
            ts = parser(row[key])
            if ts is not None:
                return ts
    return None


def chronological_split(rows: list[dict]) -> tuple[list[int], list[int]]:
    """(train_indices, test_indices) over ``rows`` in row order.

    Indices are sorted by timestamp (rows without one sort LAST, keeping
    their original relative order — a stable sort); the earlier half
    trains, the later half tests. With no parseable timestamps at all this
    degenerates to positional first-half/second-half of the given order.
    """
    n = len(rows)
    times = [row_time(row) for row in rows]
    order = sorted(
        range(n),
        key=lambda i: (times[i] if times[i] is not None else math.inf, i),
    )
    n_train = n // 2
    return order[:n_train], order[n_train:]


def _import_stack() -> SimpleNamespace:
    """The numpy/sklearn pieces the battery needs, or ImportError — the
    honest gate for environments without the ``signals[lab]`` extra."""
    import numpy as np
    from sklearn import __version__ as sklearn_version
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import average_precision_score, roc_auc_score
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    return SimpleNamespace(
        np=np,
        sklearn_version=tuple(int(p) for p in sklearn_version.split(".")[:2]),
        LogisticRegression=LogisticRegression,
        Pipeline=Pipeline,
        StandardScaler=StandardScaler,
        roc_auc_score=roc_auc_score,
        average_precision_score=average_precision_score,
    )


def _elasticnet_model(stack: SimpleNamespace) -> Any:
    """Elastic-net logistic (l1_ratio=0.5), constructed version-portably:
    sklearn >= 1.8 deprecates ``penalty="elasticnet"`` in favor of passing
    ``l1_ratio`` alone, while older sklearn (< 1.8) ignores ``l1_ratio``
    unless the penalty is explicitly elasticnet."""
    common = {"solver": "saga", "l1_ratio": 0.5, "C": 1.0, "max_iter": 5000, "random_state": 0}
    if getattr(stack, "sklearn_version", (0, 0)) >= (1, 8):
        return stack.LogisticRegression(**common)
    return stack.LogisticRegression(penalty="elasticnet", **common)


def _fit_score(
    stack: SimpleNamespace, X: Any, y: Any, train_idx: list[int], test_idx: list[int]
) -> dict:
    """Elastic-net logistic (standardized — raw feature scales span orders
    of magnitude) fit on the train half, AUROC + PR-AUC on the test half.
    Undefined metrics are honest Nones with a note, never crashes."""
    base = {
        "auroc": None,
        "pr_auc": None,
        "n": int(len(train_idx) + len(test_idx)),
        "n_train": len(train_idx),
        "n_test": len(test_idx),
    }
    if len(train_idx) < 2 or len(test_idx) < 1 or getattr(X, "shape", (0, 1))[1] == 0:
        return {**base, "note": "split too small or no features"}
    y_train, y_test = y[train_idx], y[test_idx]
    if len(set(y_train.tolist())) < 2:
        return {**base, "note": "single-class train split"}
    try:
        model = stack.Pipeline(
            [("scale", stack.StandardScaler()), ("lr", _elasticnet_model(stack))]
        )
        model.fit(X[train_idx], y_train)
        proba = model.predict_proba(X[test_idx])[:, 1]
        auroc = float(stack.roc_auc_score(y_test, proba))
        pr_auc = float(stack.average_precision_score(y_test, proba))
    except ValueError as exc:  # e.g. single-class test split: AUROC undefined
        return {**base, "note": f"metrics undefined: {exc}"}
    except Exception as exc:  # fit failure is a family-level fact, not a crash
        return {**base, "note": f"fit failed: {exc}"}
    if math.isnan(auroc) or math.isnan(pr_auc):
        # Newer sklearn warns-and-NaNs (instead of raising) on a single-class
        # test split — an undefined metric is an honest None, never a NaN.
        return {**base, "note": "metrics undefined: single-class test split"}
    return {
        "auroc": auroc,
        "pr_auc": pr_auc,
        **{k: v for k, v in base.items() if k not in ("auroc", "pr_auc")},
    }


def _best_single(results: dict) -> tuple[str | None, dict | None]:
    """The best-scoring single family (by AUROC, then PR-AUC). The
    ``combined`` stack and metadata keys are never candidates — the mandate
    is about beating a SINGLE family, and stacking everything is not one."""
    best_name, best = None, None
    for name, entry in results.items():
        if name == "combined" or not isinstance(entry, dict):
            continue
        if entry.get("auroc") is None:
            continue
        key = (entry["auroc"], entry.get("pr_auc") or 0.0)
        if best is None or key > (best["auroc"], best.get("pr_auc") or 0.0):
            best_name, best = name, entry
    return best_name, best


def score_baselines(
    families: dict[str, tuple[Any, Any]], rows: list[dict] | None = None
) -> dict:
    """Score the baseline battery.

    ``families`` maps family name -> ``(X, y)`` where X is the family's
    feature matrix (numpy array or None when the family is unavailable,
    e.g. the embedding family under a NullEmbedder) and y the shared 0/1
    labels in the same row order for every family. ``rows`` are the corpus
    rows that produced the matrices (same order); they drive the
    chronological split — omit them (or pass a mismatched count) and the
    positional fallback applies.

    Returns ``{family: {"auroc", "pr_auc", "n", "n_train", "n_test"}, ...}``
    plus a ``combined`` entry (when >= 2 families are available), the
    ``split`` label, and the ``mandate`` line naming the best single
    family. Degenerate inputs return an honest ``{"error": ...}`` dict —
    most importantly ``{"error": "sklearn unavailable"}`` without the lab
    extra.
    """
    try:
        stack = _import_stack()
    except ImportError:
        return {"error": "sklearn unavailable"}
    np = stack.np

    y: Any = None
    n = 0
    for name, pair in families.items():
        labels = np.asarray(pair[1])
        if y is None:
            y = labels.astype(int)
            n = int(y.shape[0])
        elif labels.shape[0] != n or not np.array_equal(labels, y):
            return {"error": f"family {name!r} labels differ from the first family"}
    if y is None or n == 0:
        return {"error": "no families"}
    if not set(np.unique(y).tolist()) <= {0, 1}:
        return {"error": "labels must be 0/1"}

    if rows is not None and len(rows) == n:
        train_idx, test_idx = chronological_split(rows)
        times_all = all(row_time(row) is not None for row in rows)
        split_label = SPLIT_CHRONOLOGICAL if times_all else SPLIT_FALLBACK
    else:
        order = list(range(n))
        n_train = n // 2
        train_idx, test_idx = order[:n_train], order[n_train:]
        split_label = SPLIT_FALLBACK

    results: dict[str, dict] = {}
    available: dict[str, Any] = {}
    for name, (X, _labels) in families.items():
        if X is None:
            results[name] = {
                "auroc": None,
                "pr_auc": None,
                "n": n,
                "n_train": len(train_idx),
                "n_test": len(test_idx),
                "unavailable": True,
            }
            continue
        results[name] = _fit_score(stack, np.asarray(X, dtype=float), y, train_idx, test_idx)
        available[name] = X

    if len(available) >= 2:  # then combined: every live family stacked
        combined = np.hstack(
            [np.asarray(available[name], dtype=float) for name in sorted(available)]
        )
        results["combined"] = _fit_score(stack, combined, y, train_idx, test_idx)

    results["split"] = split_label
    best_name, best = _best_single(results)
    if best is not None:
        results["mandate"] = (
            f"{MANDATE} Best single family: {best_name} "
            f"(AUROC {best['auroc']:.3f}, PR-AUC {best['pr_auc']:.3f}, {split_label} split)."
        )
    else:
        results["mandate"] = f"{MANDATE} No single family scored on the {split_label} split."
    return results
