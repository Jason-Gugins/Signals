"""Task 6 (decide-lab): the 4-noul question prescreener + correlation pruning.

Spec: docs/plans/2026-10-11-autoresearch-decide-lab.md — a candidate gate
question never reaches a paid A/B round until it clears the cookbook's four
zero-policy Nouls (answerable_from_source / one_meaning_under_criteria /
applies_to_most_rows / varies_across_rows) on a stratified 30-doc sample;
surviving questions whose dev-corpus noul columns are near-duplicates
(|Pearson r| > 0.85) are pruned to the clearest member of each cluster.

Fully OFFLINE: every decider is an in-process fake (tests/test_decide_jev.py
patterns — scripted Decisions, never a network call; the conftest
_no_network autouse guard backs this up).
"""

from __future__ import annotations

import pytest

from src.decide import Decision, NullDecider
from src.lab.prescreen import (
    DEFAULT_FLOOR,
    GATE,
    SCREENING_NOULS,
    prescreen,
    prune_correlated,
    sample_digest,
)

NOULS = SCREENING_NOULS  # the four names, imported so tests cannot drift

SAMPLE_STATES = [
    {"doc_id": f"doc-{i}", "source": "Acme", "text": f"staged document {i} " * 40}
    for i in range(30)
]

CANDIDATES = [
    {"text": "Does the document state an operational pain?", "criteria": "yes when a pain is named"},
    {"text": "Does the document name a competitor?", "criteria": "yes when a rival company appears"},
]


def all_clear(score: float = 0.9, **over: float) -> Decision:
    """An ok Decision answering all four Nouls (per-name overrides allowed)."""
    answers = {n: {"noul": float(over.get(n, score))} for n in NOULS}
    return Decision(applies=True, ok=True, answers=answers, raw_tokens=17)


class FakeDecider:
    """A three-arg decider double: scripted Decisions keyed by the candidate's
    question text, every call recorded (state/questions/gate)."""

    model = "jev-fake"

    def __init__(self, decisions: dict | None = None, default: Decision | None = None) -> None:
        self.decisions = dict(decisions or {})
        self.default = default if default is not None else all_clear()
        self.calls: list[dict] = []

    def decide(self, state, questions, gate=None):  # three-arg shape (the shipped one)
        self.calls.append({"state": state, "questions": questions, "gate": gate})
        question = state["question"] if isinstance(state, dict) else str(state)
        return self.decisions.get(question, self.default)


class TwoArgFakeDecider(FakeDecider):
    """The older two-argument signature (every test fake, third-party impls) —
    prescreen must call it WITHOUT the gate kwarg (gates.py _call's rule)."""

    def decide(self, state, questions):  # noqa: D102 - two-arg override
        self.calls.append({"state": state, "questions": questions, "gate": "<not passed>"})
        question = state["question"] if isinstance(state, dict) else str(state)
        return self.decisions.get(question, self.default)


# ------------------------------------------------------------------ prescreen


def test_all_four_clear_passes():
    decider = FakeDecider()
    survivors, rows = prescreen(CANDIDATES, SAMPLE_STATES, decider, run_id="run-x")

    assert len(survivors) == 2
    assert len(rows) == 2  # one honest row per candidate
    for cand, survivor, row in zip(CANDIDATES, survivors, rows):
        assert survivor["question"] == cand["text"]  # canonical key, not "text"
        assert set(survivor["nouls"]) == set(NOULS)
        assert survivor["screening_score"] == pytest.approx(sum(survivor["nouls"].values()))
        assert "nouls" not in cand  # the source candidate is never mutated
        assert row["passed"] is True
        assert row["outcome"] == "passed"
        assert row["failing"] == []
        assert row["cause"] is None
        assert row["run_id"] == "run-x"
        assert row["gate"] == GATE
        assert row["raw_tokens"] == 17
        assert row["latency_ms"] >= 0
    # One batched request per candidate — exactly, no per-noul calls.
    assert len(decider.calls) == 2


def test_request_shape_state_carries_sample_digest():
    decider = FakeDecider()
    prescreen(CANDIDATES, SAMPLE_STATES, decider)

    assert len(decider.calls) == 2
    for call in decider.calls:
        state = call["state"]
        assert isinstance(state, dict)
        assert set(state) == {"question", "criteria", "sample_digest"}
        # The four zero-policy Nouls: noul-typed, no criteria on the question.
        assert set(call["questions"]) == set(NOULS)
        for spec in call["questions"].values():
            assert spec["type"] == "noul"
            assert "criteria" not in spec
        # The digest describes the sample it was evaluated on.
        digest = state["sample_digest"]
        assert digest["n"] == len(SAMPLE_STATES)
        assert len(digest["fingerprint"]) == 64  # sha256 hex
        assert len(digest["previews"]) == len(SAMPLE_STATES)
    # Same sample -> same fingerprint across candidates and calls.
    fp1, fp2 = (c["state"]["sample_digest"]["fingerprint"] for c in decider.calls)
    assert fp1 == fp2
    assert sample_digest(SAMPLE_STATES)["fingerprint"] == fp1
    # A different sample digests differently.
    assert sample_digest(SAMPLE_STATES[:-1])["fingerprint"] != fp1


def test_one_below_floor_fails_with_the_noul_named():
    low = all_clear(varies_across_rows=0.40)  # clearly under the 0.60 floor
    decider = FakeDecider(decisions={CANDIDATES[0]["text"]: low})
    survivors, rows = prescreen(CANDIDATES, SAMPLE_STATES, decider)

    assert [s["question"] for s in survivors] == [CANDIDATES[1]["text"]]
    row = rows[0]
    assert row["passed"] is False
    assert row["outcome"] == "failed_floor"
    assert row["failing"] == ["varies_across_rows"]
    assert row["nouls"]["varies_across_rows"] == pytest.approx(0.40)
    assert row["missing"] == []


def test_missing_noul_answer_cannot_clear_its_floor():
    answers = {n: {"noul": 0.9} for n in NOULS}
    del answers["applies_to_most_rows"]  # a noul the decider never answered
    decider = FakeDecider(default=Decision(applies=True, ok=True, answers=answers))
    survivors, rows = prescreen([CANDIDATES[0]], SAMPLE_STATES, decider)

    assert survivors == []
    assert rows[0]["outcome"] == "failed_floor"
    assert rows[0]["failing"] == ["applies_to_most_rows"]
    assert rows[0]["missing"] == ["applies_to_most_rows"]
    assert rows[0]["nouls"]["applies_to_most_rows"] is None


def test_failed_decision_is_honest_fail_with_cause():
    failed = Decision(
        applies=True, ok=False, answers={}, raw_tokens=0,
        cause="ConnectError: connection refused",
    )
    decider = FakeDecider(decisions={CANDIDATES[0]["text"]: failed})
    survivors, rows = prescreen(CANDIDATES, SAMPLE_STATES, decider)

    # The failed candidate is out — with the decision's cause on its row...
    assert [s["question"] for s in survivors] == [CANDIDATES[1]["text"]]
    assert rows[0]["passed"] is False
    assert rows[0]["outcome"] == "failed_decision"
    assert rows[0]["cause"] == "ConnectError: connection refused"
    # ...and screening continued past the failure (never raises past a candidate).
    assert rows[1]["passed"] is True


def test_not_applicable_decision_fails_honestly():
    # NullDecider (layer off): applies=False — nothing clears, nothing raises.
    survivors, rows = prescreen([CANDIDATES[0]], SAMPLE_STATES, NullDecider())
    assert survivors == []
    assert rows[0]["passed"] is False
    assert rows[0]["outcome"] == "failed_decision"
    assert rows[0]["cause"]


def test_decider_crash_is_honest_fail_not_exception():
    class CrashingOnOne:
        """Crashes only on the named candidate — the seam's never-raise
        contract is the decider's job; this double breaks it deliberately."""

        def decide(self, state, questions, gate=None):
            if state["question"] == "crash candidate":
                raise RuntimeError("boom past the seam")
            return all_clear()

    mixed = [CANDIDATES[0], {"text": "crash candidate", "criteria": ""}, CANDIDATES[1]]
    survivors, rows = prescreen(mixed, SAMPLE_STATES, CrashingOnOne())

    assert rows[1]["passed"] is False
    assert rows[1]["outcome"] == "failed_decision"
    assert "RuntimeError" in rows[1]["cause"]
    # The candidates after the crash were still screened.
    assert rows[2]["passed"] is True
    assert len(survivors) == 2


def test_two_arg_decider_tolerated():
    decider = TwoArgFakeDecider()
    survivors, rows = prescreen(CANDIDATES, SAMPLE_STATES, decider)

    assert len(survivors) == 2  # no TypeError: the gate kwarg was withheld
    assert all(c["gate"] == "<not passed>" for c in decider.calls)
    # ...while the three-arg decider DOES receive the gate (breaker threading).
    three = FakeDecider()
    prescreen([CANDIDATES[0]], SAMPLE_STATES, three)
    assert three.calls[0]["gate"] == "lab_prescreen"


def test_partial_floors_override_per_noul():
    decider = FakeDecider()  # every noul at 0.90
    survivors, rows = prescreen(
        [CANDIDATES[0]], SAMPLE_STATES, decider,
        floors={"varies_across_rows": 0.95},  # only this noul's floor moves
    )
    assert survivors == []
    row = rows[0]
    assert row["outcome"] == "failed_floor"
    assert row["failing"] == ["varies_across_rows"]
    assert row["floors"]["varies_across_rows"] == 0.95
    for n in NOULS:
        if n != "varies_across_rows":
            assert row["floors"][n] == DEFAULT_FLOOR  # 0.60 default untouched


def test_blank_candidate_skipped_without_a_decider_call():
    decider = FakeDecider()
    survivors, rows = prescreen(
        [{"text": "   ", "criteria": "blank"}, *CANDIDATES], SAMPLE_STATES, decider
    )
    assert rows[0]["outcome"] == "skipped"
    assert rows[0]["passed"] is False
    assert len(survivors) == 2
    assert len(decider.calls) == 2  # no request paid for the blank


def test_default_floor_is_point_sixty():
    row_floors = prescreen([CANDIDATES[0]], SAMPLE_STATES, FakeDecider())[1][0]["floors"]
    assert set(row_floors) == set(NOULS)
    assert all(f == DEFAULT_FLOOR for f in row_floors.values()) and DEFAULT_FLOOR == 0.60


# ---------------------------------------------------------------- prune


def qr(question: str, scores: list, total: float, criteria: str = "c") -> dict:
    """One encoded question result: screening Nouls (summing to ``total``) +
    the per-corpus-row noul column (None = not answered on that row)."""
    share = total / len(NOULS)
    return {
        "question": question,
        "criteria": criteria,
        "nouls": {n: share for n in NOULS},
        "screening_score": total,
        "scores": list(scores),
    }


def test_prune_keeps_highest_scoring_member_of_dup_cluster():
    col = [0.1, 0.9, 0.2, 0.8, 0.3, 0.7]
    results = [
        qr("weak dup", col, total=2.8),
        qr("clearest dup", col, total=3.6),
        qr("middling dup", col, total=3.2),
    ]
    kept = prune_correlated(results, threshold=0.85)
    assert [k["question"] for k in kept] == ["clearest dup"]


def test_prune_tie_keeps_shorter_prompt():
    col = [0.2, 0.8, 0.2, 0.8]
    results = [
        qr("a much longer question text than its twin", col, total=3.0, criteria="long criteria " * 3),
        qr("short twin", col, total=3.0),
    ]
    kept = prune_correlated(results)
    assert [k["question"] for k in kept] == ["short twin"]  # cheaper prompt wins


def test_prune_independent_columns_all_kept():
    varying = [0.1, 0.9, 0.1, 0.9, 0.1, 0.9]
    weakly_related = [0.2, 0.1, 0.9, 0.8, 0.15, 0.85]  # |r| ~ 0.24
    constant = [0.5] * 6  # zero variance: Pearson r undefined -> no edge
    results = [
        qr("varying", varying, 3.6),
        qr("weakly related", weakly_related, 3.0),
        qr("constant", constant, 2.0),
    ]
    kept = prune_correlated(results, threshold=0.85)
    assert [k["question"] for k in kept] == ["varying", "weakly related", "constant"]


def test_prune_correlates_over_rows_where_both_answered():
    # Disjoint answer coverage: no overlap -> r undefined -> both kept.
    results = [
        qr("left half", [0.1, 0.9, None, None], 3.0),
        qr("right half", [None, None, 0.2, 0.8], 3.0),
    ]
    assert len(prune_correlated(results)) == 2

    # Perfect ANTI-correlation on the overlap: |r| == 1 -> clustered, keep one.
    anti = [
        qr("up", [0.1, 0.9, 0.1, None], 3.2),
        qr("down", [0.9, 0.1, 0.9, None], 2.4),
    ]
    assert [k["question"] for k in prune_correlated(anti)] == ["up"]


def test_prune_empty_and_singleton():
    assert prune_correlated([]) == []
    solo = [qr("solo", [0.1, 0.9], 3.0)]
    assert prune_correlated(solo) == solo


def test_prune_preserves_order_and_never_mutates_input():
    col = [0.3, 0.7, 0.3, 0.7]
    # [0.9, 0.1, 0.1, 0.9] vs col: r == 0 (genuinely uncorrelated patterns).
    results = [
        qr("independent", [0.9, 0.1, 0.1, 0.9], 3.0),
        qr("dup low", col, 2.5),
        qr("dup high", col, 3.5),
    ]
    snapshot = [dict(r) for r in results]
    kept = prune_correlated(results)
    assert [k["question"] for k in kept] == ["independent", "dup high"]  # input order
    assert results == snapshot  # input untouched (pure function)
