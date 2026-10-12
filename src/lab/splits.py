"""Validation splits that match deployment (decide-lab Task 8).

Three split families, each returning folds that carry their split LABEL so
every reported number can name the split that produced it (the honest-
numbers invariant):

* :func:`chronological_folds` — the PRIMARY split: expanding-window folds
  over run time (``generated_at`` / package timestamp / ``fetched_at`` via
  :func:`src.lab.baselines.row_time` — the same parsing and the same
  no-timestamp-sorts-last policy as the Task 4 baseline split). The rows
  are cut into ``n_splits + 1`` contiguous time blocks; fold i trains on
  blocks 0..i and tests on block i+1, so the train side is always
  STRICTLY earlier than the test side — deployment predicts future runs,
  and validation must not peek at them.
* :func:`grouped_folds` — GroupKFold semantics on the rows' ``domain``
  (stdlib implementation of the classic algorithm: heaviest domain first,
  each assigned to the lightest fold, lowest fold index on ties —
  deterministic). A domain never straddles train/test: no account leaks
  across the split. This is the discovery/cross-validation split.
* :func:`final_holdout` — the most recent ``frac`` of runs by time,
  returned ONCE and marked ``"holdout"``. The contract this label marks:
  nothing upstream — question discovery, prescreening, model selection,
  recalibration tuning — may so much as look at the holdout rows; they are
  scored exactly once, at the very end, by the final chosen configuration.

All index lists reference positions in the ORIGINAL ``rows`` list.
Chronological/holdout index lists appear in TIME order; grouped-fold lists
are sorted by row index. Module top stays stdlib-only (the lab-extra
scoring lives in :mod:`src.lab.models`).
"""

from __future__ import annotations

import math

from src.lab.baselines import row_time  # stdlib-top module: safe anywhere

LABEL_CHRONOLOGICAL = "chronological"
LABEL_GROUPED = "grouped"
LABEL_HOLDOUT = "holdout"


def _time_order(rows: list[dict]) -> list[int]:
    """Row indices sorted by :func:`src.lab.baselines.row_time` (rows
    without a parseable timestamp sort LAST — treated as most recent —
    keeping their original relative order via the stable sort)."""
    n = len(rows)
    times = [row_time(row) for row in rows]
    return sorted(range(n), key=lambda i: (times[i] if times[i] is not None else math.inf, i))


def _blocks(order: list[int], k: int) -> list[list[int]]:
    """``order`` cut into ``k`` contiguous, near-equal blocks
    (``numpy.array_split`` semantics: the first ``len(order) % k`` blocks
    get one extra element)."""
    base, rem = divmod(len(order), k)
    out: list[list[int]] = []
    start = 0
    for i in range(k):
        size = base + (1 if i < rem else 0)
        out.append(order[start : start + size])
        start += size
    return out


def chronological_folds(rows: list[dict], n_splits: int) -> list[dict]:
    """Expanding-window chronological folds — the primary split.

    ``n_splits + 1`` contiguous time blocks over the time-sorted rows;
    fold i trains on every block strictly earlier than its test block, so
    training NEVER sees the future relative to its test (verified by tests
    on actual timestamps, not just positions). Train lists grow block by
    block (an expanding window); the last fold trains on everything
    except the newest block. Every fold carries
    ``{"label": "chronological", "fold": i, "train": [...], "test": [...]}``
    with indices in TIME order.
    """
    if n_splits < 1:
        raise ValueError("n_splits must be >= 1")
    n = len(rows)
    if n < n_splits + 1:
        raise ValueError(
            f"chronological_folds needs at least n_splits+1 = {n_splits + 1} rows, got {n}"
        )
    order = _time_order(rows)
    blocks = _blocks(order, n_splits + 1)
    folds: list[dict] = []
    boundary = len(blocks[0])
    for i in range(n_splits):
        test = blocks[i + 1]
        folds.append(
            {
                "label": LABEL_CHRONOLOGICAL,
                "fold": i,
                "train": order[:boundary],
                "test": test,
            }
        )
        boundary += len(test)
    return folds


def grouped_folds(rows: list[dict], n_splits: int) -> list[dict]:
    """GroupKFold-semantics folds on the rows' ``domain`` — the discovery
    and cross-validation split (no account leakage).

    The classic greedy assignment in stdlib: domains sorted heaviest-first
    (count desc, then first appearance, then name — fully deterministic),
    each assigned to the fold carrying the fewest rows so far (lowest fold
    index on ties). Every row of a domain lands in exactly one fold's test
    set; that fold's train set is every other domain's rows. Folds carry
    ``{"label": "grouped", "fold": k, "train": [...], "test": [...]}``
    with indices sorted by row position.
    """
    if n_splits < 2:
        raise ValueError("grouped_folds needs n_splits >= 2 (GroupKFold semantics)")
    domains = [str(row.get("domain") or "unknown") for row in rows]
    members: dict[str, list[int]] = {}
    for i, domain in enumerate(domains):
        members.setdefault(domain, []).append(i)
    if len(members) < n_splits:
        raise ValueError(
            f"grouped_folds needs at least {n_splits} distinct domains, got {len(members)}"
        )

    assignment: dict[str, int] = {}
    weights = [0] * n_splits
    for domain in sorted(members, key=lambda d: (-len(members[d]), members[d][0], d)):
        lightest = min(range(n_splits), key=lambda k: weights[k])
        assignment[domain] = lightest
        weights[lightest] += len(members[domain])

    folds: list[dict] = []
    for k in range(n_splits):
        test_groups = {d for d, fold in assignment.items() if fold == k}
        folds.append(
            {
                "label": LABEL_GROUPED,
                "fold": k,
                "train": sorted(i for i, d in enumerate(domains) if d not in test_groups),
                "test": sorted(i for i, d in enumerate(domains) if d in test_groups),
            }
        )
    return folds


def final_holdout(rows: list[dict], frac: float = 0.2) -> dict:
    """The final test set — the most recent runs, returned ONCE, marked.

    The latest ``round(frac * n)`` rows by run time (at least 1, at most
    ``n - 1`` so the working set is never empty); everything earlier is
    the train side. The ``"holdout"`` label marks the contract: these rows
    are untouched by discovery AND model selection — the loop proposes,
    screens, selects and tunes entirely on the train side, and the holdout
    is scored exactly once, at the very end, by the final configuration.
    Both index lists are in TIME order.
    """
    if not 0.0 < frac < 1.0:
        raise ValueError("frac must be in (0, 1)")
    n = len(rows)
    if n < 2:
        raise ValueError("final_holdout needs at least 2 rows to carve off")
    order = _time_order(rows)
    n_hold = max(1, min(int(round(frac * n)), n - 1))
    cut = n - n_hold
    return {
        "label": LABEL_HOLDOUT,
        "train": order[:cut],
        "test": order[cut:],
    }
