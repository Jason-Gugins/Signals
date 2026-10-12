"""Task 5 (decide-lab): the stage-variant registry + weighted replay scorer.

Spec: docs/plans/2026-10-11-autoresearch-decide-lab.md — A/B ONE stage at a
time against recorded states: doc-gate variants re-run the gate_document
question set on the joined 9,600-char doc heads, G4 variants re-run the
citation Choice on persisted claims + quote-anchored excerpts, implementer
prompt variants are metadata-only (deepseek, out of replay scope), and every
replay is scored by the weighted battery
(w1*acceptance_precision + w2*claim_recall + w3*promotion_proxy
- w4*cost_calls - w5*latency_s_norm) against the rows' RECORDED outcomes as
ground truth.

Fully OFFLINE: every decider is an in-process fake (the tests/test_decide_jev.py
patterns — scripted Decisions, never a network call; the conftest _no_network
autouse guard backs this up).

THE FAKE-STUB BLIND-SPOT LESSON, as tests: the replay is forbidden from
re-implementing the pipeline's question construction, so
``test_doc_replay_sends_the_gates_own_state_and_questions`` and
``test_g4_replay_sends_the_gates_own_state_and_questions`` run the REAL
gates with spy deciders and assert the replay's state/questions are
byte-identical to what the pipeline sent — the mirror cannot drift silently.
"""

from __future__ import annotations

import json

import pytest

from src.decide import Claim, Decision, DecideLedger, NullDecider
from src.decide import gates as gates_mod
from src.decide.gates import (
    CLAIM_FLOOR,
    DOC_EVIDENCE_MIN,
    DOC_RELEVANT_MIN,
    OUTPUT_ACTION_THRESHOLD,
    OUTPUT_REVIEW_THRESHOLD,
    gate_citation_batch,
    gate_document,
    state_hash,
)
from src.lab.replay import (
    DEFAULT_WEIGHTS,
    G4_BAND_LOW_DEFAULT,
    advance,
    replay_variant,
)
from src.lab.variants import (
    PROFILE_VOCAB_QUESTION_ID,
    REQUIRED_KEYS,
    apply_variant,
    default_variants,
)

# --------------------------------------------------------------------- doubles


class SpyDecider:
    """A three-arg decider double: ``fn(state, questions) -> Decision`` with
    every call recorded (state/questions/gate) — the spy the
    construction-identity tests read the pipeline's exact request from."""

    model = "jev-fake"

    def __init__(self, fn) -> None:
        self.fn = fn
        self.calls: list[dict] = []

    def decide(self, state, questions, gate=None):
        self.calls.append({"state": state, "questions": questions, "gate": gate})
        return self.fn(state, questions)


class TwoArgSpyDecider(SpyDecider):
    """The older two-argument signature — replay must call it WITHOUT the
    gate kwarg (gates.py _call's rule, mirrored by the lab)."""

    def decide(self, state, questions):  # noqa: D102 - two-arg override
        self.calls.append({"state": state, "questions": questions, "gate": "<not passed>"})
        return self.fn(state, questions)


class CrashingOnFirst:
    """Breaks the seam's never-raise contract on one doc deliberately — the
    replay's belt must collapse the crash into an honest no-verdict row."""

    model = "jev-crash"

    def decide(self, state, questions, gate=None):
        if state["doc_id"] == "d-crash":
            raise RuntimeError("boom past the seam")
        return ok(doc_answers(0.9, 0.8))


def ok(answers: dict, raw_tokens: int = 23) -> Decision:
    return Decision(applies=True, ok=True, answers=answers, raw_tokens=raw_tokens)


def failed(cause: str = "ConnectError: connection refused") -> Decision:
    return Decision(applies=True, ok=False, answers={}, raw_tokens=0, cause=cause)


def doc_answers(relevant: float, evidence: float, injection: float = 0.05, **extra) -> dict:
    """A gate_document-shaped answers map (the three shipped Nouls + extras)."""
    answers = {
        "is_relevant": {"noul": relevant},
        "contains_signal_evidence": {"noul": evidence},
        "contains_prompt_injection": {"noul": injection},
    }
    answers.update(extra)
    return answers


def by_doc_id(per_doc: dict, default: Decision | None = None) -> SpyDecider:
    """Answers keyed by the state's doc_id (a good-docs default for unknowns)."""
    if default is None:
        default = ok(doc_answers(0.9, 0.8))

    def fn(state, questions):
        return per_doc.get(state["doc_id"], default)

    return SpyDecider(fn)


# ------------------------------------------------------------------- fixtures

DOC_TEXT = (
    "Acme Corp press release. Acme reported revenue of $4.2m in Q3 2026, up 31% "
    "year over year, and named a new CISO. " + "Routine filler paragraph. " * 80
)

G4_DOC = (
    "Acme Corp annual report. " + "Background filler sentence. " * 60
    + "Acme reported revenue of $4.2m in Q3 2026, up 31% year over year. "
    + "The board contradicted the earlier layoff rumor outright. "
    + "The cafeteria menu mentions pasta. " + "Trailing filler sentence. " * 60
)


def doc_row(
    doc_id: str,
    text: str,
    outcome: str,
    *,
    account: str = "Acme",
    answers: dict | None = None,
    joined: bool = True,
    run_id: str = "run-1",
) -> dict:
    """One corpus-shaped document_gate row (corpus.extract's joined shape);
    ``state_hash`` is built exactly the way corpus._DocIndex hashed the head,
    so the construction-identity tests can pin the replay to it."""
    from src.core.textutil import truncate

    head = truncate(text, 9600) or ""
    state = {"doc_id": doc_id, "source": account, "text": head}
    return {
        "gate": "document_gate",
        "boundary": "document_gate",
        "run_id": run_id,
        "package": "acme.com-20261011T000000Z-run-1",
        "domain": "acme.com",
        "account": account,
        "doc_id": doc_id if joined else None,
        "text": head if joined else None,
        "doc_joined": joined,
        "outcome": outcome,
        "state_hash": state_hash(state),
        "answers": answers,
    }


def claim_rows(
    outcomes: dict[int, tuple[str, dict]],
    doc_id: str = "doc-g4",
    *,
    n_claims: int = 3,
    package: str = "acme.com-20261011T000000Z-run-1",
    batch: str = "batch-1",
) -> list[dict]:
    """Citation rows for ONE judged batch: ``outcomes`` maps claim idx ->
    (recorded outcome, the answer the ledger row carried). All rows share the
    batch ``state_hash`` (any equal string groups them — grouping is by
    equality, never by validity)."""
    claims = [
        {"text": f"claim {i}", "quote_span": f"quote {i}", "doc_id": doc_id}
        for i in range(n_claims)
    ]
    rows = []
    for i, (outcome, answer) in sorted(outcomes.items()):
        rows.append(
            {
                "gate": "citation_soundness",
                "boundary": "citation_soundness",
                "run_id": "run-1",
                "package": package,
                "domain": "acme.com",
                "account": "Acme",
                "doc_id": doc_id,
                "state_hash": batch,
                "outcome": outcome,
                "answers": {f"claim_{i}": answer},
                "claims": claims,
            }
        )
    return rows


def g4_answer(label: str, prob: float) -> dict:
    """One Choice answer in the shape _choice_of/_choice_prob read."""
    return {"choice": label, "probabilities": {label: prob}}


def variant_by_id(vid: str) -> dict:
    return next(v for v in default_variants() if v["id"] == vid)


def _rows_of(result: dict) -> list[dict]:
    """The per-unit replay rows carried on the result (also exactly what
    lands in the out_dir JSONL when one is given)."""
    return result["rows"]


def _outcomes(result: dict) -> list[str]:
    return [r["outcome"] for r in _rows_of(result)]


# ------------------------------------------------------------ registry shape


def test_registry_covers_the_three_stages_with_unique_ids():
    variants = default_variants()
    assert variants
    ids = [v["id"] for v in variants]
    assert len(set(ids)) == len(ids)  # unique
    assert {v["stage"] for v in variants} == {"doc_gate", "g4", "implementer"}


def test_registry_entry_shape_pinned():
    for v in default_variants():
        assert set(REQUIRED_KEYS) <= set(v), v["id"]
        assert isinstance(v["id"], str) and v["id"]
        assert isinstance(v["description"], str) and v["description"]
        assert isinstance(v["config_delta"], dict)
        assert v["requires_replay"] in ("jev", "deepseek", None)
        # expected_direction: a compact direction token or None
        assert v["expected_direction"] is None or isinstance(v["expected_direction"], str)
        # the decide.yaml gate block the delta merges over is always named
        assert v["block"] in (
            "document_gate", "citation_soundness", "output_screen",
            "implementers.bulk", "routing",
        )


def test_registry_threshold_ladders_bracket_the_shipped_values():
    docs = [v for v in default_variants() if v["stage"] == "doc_gate"]
    rel = [v["config_delta"]["relevant_min"] for v in docs if "relevant_min" in v["config_delta"]]
    ev = [v["config_delta"]["evidence_min"] for v in docs if "evidence_min" in v["config_delta"]]
    assert rel and min(rel) < DOC_RELEVANT_MIN < max(rel)
    assert ev and min(ev) < DOC_EVIDENCE_MIN < max(ev)

    g4s = [v for v in default_variants() if v["stage"] == "g4"]
    floors = [v["config_delta"]["floor"] for v in g4s if "floor" in v["config_delta"]]
    assert floors and min(floors) < CLAIM_FLOOR < max(floors)
    # the band ladder carries alternates around the shipped 0.30
    bands = [v["config_delta"]["band_low"] for v in g4s if "band_low" in v["config_delta"]]
    assert bands and any(b < G4_BAND_LOW_DEFAULT for b in bands)
    assert any(b > G4_BAND_LOW_DEFAULT for b in bands)
    assert G4_BAND_LOW_DEFAULT == gates_mod._BAND_LOW  # pinned to gates.py's own default

    # output_screen threshold ladders bracket the shipped screen thresholds
    screens = [v for v in g4s if v["block"] == "output_screen"]
    assert screens
    reviews = [v["config_delta"]["review_threshold"] for v in screens if "review_threshold" in v["config_delta"]]
    actions = [v["config_delta"]["action_threshold"] for v in screens if "action_threshold" in v["config_delta"]]
    assert reviews and min(reviews) < OUTPUT_REVIEW_THRESHOLD < max(reviews)
    assert actions and any(a != OUTPUT_ACTION_THRESHOLD for a in actions)


def test_registry_doc_gate_has_the_4th_noul_variant():
    docs = [v for v in default_variants() if v["stage"] == "doc_gate"]
    fourth = [
        v for v in docs
        if PROFILE_VOCAB_QUESTION_ID in (v["config_delta"].get("questions") or {})
    ]
    assert fourth, "the has_profile_vocab_evidence 4th-question variant is registered"
    spec = fourth[0]["config_delta"]["questions"][PROFILE_VOCAB_QUESTION_ID]
    assert spec["type"] == "noul"
    assert spec["instructions"].strip()


def test_registry_implementer_variants_are_deepseek_metadata_only():
    impl = [v for v in default_variants() if v["stage"] == "implementer"]
    assert impl
    for v in impl:
        assert v["requires_replay"] == "deepseek"  # out of replay scope by design


def test_default_variants_returns_fresh_copies():
    first = default_variants()
    first[0]["config_delta"]["__poison__"] = True
    second = default_variants()
    assert "__poison__" not in second[0]["config_delta"]


# -------------------------------------------------------------- apply_variant


def test_apply_variant_shallow_merge_is_pure():
    base = {"floor": 0.70, "band_low": 0.30, "enabled": True, "nested": {"a": 1}}
    variant = {"id": "x", "config_delta": {"floor": 0.60, "nested": {"b": 2}}}
    merged = apply_variant(base, variant)

    assert merged == {"floor": 0.60, "band_low": 0.30, "enabled": True, "nested": {"b": 2}}
    assert merged is not base  # a NEW dict
    # purity: neither input mutated
    assert base == {"floor": 0.70, "band_low": 0.30, "enabled": True, "nested": {"a": 1}}
    assert variant["config_delta"] == {"floor": 0.60, "nested": {"b": 2}}


def test_apply_variant_empty_delta_is_the_base():
    base = {"relevant_min": 0.45}
    merged = apply_variant(base, {"id": "incumbent", "config_delta": {}})
    assert merged == base and merged is not base
    assert apply_variant(None, {"config_delta": {"floor": 0.6}}) == {"floor": 0.6}


# ------------------------------------------ construction identity (the lesson)


def test_doc_replay_sends_the_gates_own_state_and_questions():
    """The replay must exercise the SAME state/question construction the
    pipeline uses: run the REAL gate_document with a spy, then replay a
    corpus-shaped row and require byte-identical requests."""
    spy = SpyDecider(lambda state, questions: ok(doc_answers(0.9, 0.8)))
    ledger = DecideLedger()
    gate_document("doc-1", DOC_TEXT, "Acme", spy, None, ledger, "run-1", "enforce")
    assert len(spy.calls) == 1
    gate_state, gate_questions = spy.calls[0]["state"], spy.calls[0]["questions"]

    row = doc_row("doc-1", DOC_TEXT, "included")
    replay_spy = SpyDecider(lambda state, questions: ok(doc_answers(0.9, 0.8)))
    result = replay_variant(variant_by_id("doc_relevant_min_loose"), [row], replay_spy)

    assert result["status"] == "complete"
    assert len(replay_spy.calls) == 1
    assert replay_spy.calls[0]["state"] == gate_state  # byte-identical state
    assert replay_spy.calls[0]["questions"] == gate_questions  # ...and questions
    # the deterministic replay input: the replay's state hashes to what the
    # corpus row recorded (the 9,600-char head the gate hashed).
    assert state_hash(replay_spy.calls[0]["state"]) == row["state_hash"]
    assert replay_spy.calls[0]["gate"] == "lab_replay_doc_gate"


def test_doc_replay_routes_what_the_gate_routes():
    """Same decision -> same outcome: the replay's routing mirrors the real
    gate's, pinned end-to-end through gate_document's own ledger row."""
    answers = doc_answers(0.30, 0.90)  # under relevant_min -> excluded "irrelevant"
    spy = SpyDecider(lambda state, questions: ok(answers))
    ledger = DecideLedger()
    include, _, _ = gate_document("doc-9", DOC_TEXT, "Acme", spy, None, ledger, "run-1", "enforce")
    gate_outcome = ledger.rows[-1]["outcome"]
    assert include is False and gate_outcome == "excluded"

    replay_spy = SpyDecider(lambda state, questions: ok(answers))
    result = replay_variant(
        variant_by_id("doc_relevant_min_loose"),  # 0.35: the 0.30 noul still misses
        [doc_row("doc-9", DOC_TEXT, "excluded")],
        replay_spy,
    )
    assert _outcomes(result) == ["excluded"]
    assert _rows_of(result)[0]["reason"] == "irrelevant"  # same first-match-wins reason


def test_g4_replay_sends_the_gates_own_state_and_questions():
    """Same construction pin for G4: the REAL gate_citation_batch's batched
    state/questions (quote-anchored excerpts via gates.claim_excerpt) must be
    byte-identical to the replay's, and the replay must reuse claim_excerpt —
    never a parallel re-implementation."""
    claims = [
        Claim(text="Acme reported revenue of $4.2m in Q3 2026.", doc_id="doc-g4",
              quote_span="revenue of $4.2m in Q3 2026, up 31% year over year"),
        Claim(text="Acme denied the layoff rumor.", doc_id="doc-g4",
              quote_span="contradicted the earlier layoff rumor"),
        Claim(text="The cafeteria serves pasta.", doc_id="doc-g4",
              quote_span="cafeteria menu mentions pasta"),
    ]
    decision = ok({
        "claim_0": g4_answer("supports", 0.90),
        "claim_1": g4_answer("contradicted", 0.85),
        "claim_2": g4_answer("says_nothing", 0.95),
    })
    spy = SpyDecider(lambda state, questions: decision)
    ledger = DecideLedger()
    accepted, _ = gate_citation_batch(
        claims, "doc-g4", G4_DOC, spy, None, ledger, "run-1", "enforce",
        persist_claims=True,
    )
    assert len(spy.calls) == 1
    gate_state, gate_questions = spy.calls[0]["state"], spy.calls[0]["questions"]

    # corpus-shaped rows: the gate's own ledger rows + the doc-text map row
    rows = [dict(r, package="acme.com-20261011T000000Z-run-1", domain="acme.com",
                 account="Acme") for r in ledger.rows]
    rows.append(doc_row("doc-g4", G4_DOC, "included"))

    replay_spy = SpyDecider(lambda state, questions: decision)
    result = replay_variant(variant_by_id("g4_floor_loose"), rows, replay_spy)

    assert result["status"] == "complete"
    assert len(replay_spy.calls) == 1
    assert replay_spy.calls[0]["state"] == gate_state
    assert replay_spy.calls[0]["questions"] == gate_questions
    assert state_hash(replay_spy.calls[0]["state"]) == ledger.rows[0]["state_hash"]
    assert replay_spy.calls[0]["gate"] == "lab_replay_g4"


def test_g4_replay_routes_what_the_gate_routes():
    """Per-claim routing parity + the floor ladder's effect: the same Choice
    answers route identically under the shipped floor, and the tight floor
    abstains a sub-floor probability into review."""
    claims = [
        Claim(text="Revenue claim.", doc_id="doc-g4",
              quote_span="revenue of $4.2m in Q3 2026, up 31% year over year"),
        Claim(text="Layoff denial.", doc_id="doc-g4",
              quote_span="contradicted the earlier layoff rumor"),
    ]
    answers = {
        "claim_0": g4_answer("supports", 0.75),  # between 0.70 and 0.80
        "claim_1": g4_answer("says_nothing", 0.95),
    }
    spy = SpyDecider(lambda state, questions: ok(answers))
    ledger = DecideLedger()
    gate_citation_batch(claims, "doc-g4", G4_DOC, spy, None, ledger, "run-1",
                        "shadow", persist_claims=True)
    recorded = {}
    for r in ledger.rows:
        for key in (r.get("answers") or {}):
            if key.startswith("claim_"):
                recorded[key] = r["outcome"]
    assert recorded == {"claim_0": "accepted", "claim_1": "reviewed"}

    rows = [dict(r, package="p", domain="d", account="Acme") for r in ledger.rows]
    rows.append(doc_row("doc-g4", G4_DOC, "included"))

    # shipped floor (the incumbent, zero-call): reproduces the recorded routing
    incumbent = replay_variant(variant_by_id("g4_incumbent"), rows, NullDecider())
    assert incumbent["calls"] == 0
    assert _outcomes(incumbent) == ["accepted", "reviewed"]

    # tight floor: 0.75 < 0.80 -> the supports claim abstains into review
    tight = replay_variant(variant_by_id("g4_floor_tight"), rows,
                           SpyDecider(lambda s, q: ok(answers)))
    assert _outcomes(tight) == ["reviewed", "reviewed"]
    assert _rows_of(tight)[0]["reason"] == "review_label_prob"


# ------------------------------------------------------- weighted-score math


def test_weighted_score_arithmetic_is_exact():
    """score == w1*precision + w2*recall + w3*promotion_proxy
    - w4*cost_calls - w5*latency_s_norm, asserted digit-for-digit against the
    returned components."""
    rows = [
        doc_row("d1", DOC_TEXT, "included"),   # recorded positive, replay accepts -> tp
        doc_row("d2", DOC_TEXT, "included"),   # recorded positive, replay rejects -> fn
        doc_row("d3", DOC_TEXT, "excluded"),   # recorded negative, replay accepts -> fp
        doc_row("d4", DOC_TEXT, "excluded"),   # recorded negative, replay rejects -> tn
        # G5 rows ride the corpus: the promotion proxy averages their nouls
        {"gate": "need_promotion", "noul": 0.8, "run_id": "run-1", "package": "p"},
        {"gate": "need_promotion", "noul": 0.6, "run_id": "run-1", "package": "p"},
    ]
    per_doc = {
        "d1": ok(doc_answers(0.9, 0.8)),   # included
        "d2": ok(doc_answers(0.20, 0.9)),  # relevant under floor -> excluded
        "d3": ok(doc_answers(0.9, 0.8)),   # included (fp)
        "d4": ok(doc_answers(0.9, 0.10)),  # weak evidence -> excluded
    }
    result = replay_variant(variant_by_id("doc_relevant_min_loose"), rows,
                            by_doc_id(per_doc))

    assert result["status"] == "complete"
    c = result["components"]
    assert (c["tp"], c["fp"], c["fn"]) == (1, 1, 1)
    assert c["acceptance_precision"] == pytest.approx(1 / 2)
    assert c["claim_recall"] == pytest.approx(1 / 2)
    assert c["promotion_proxy"] == pytest.approx(0.7)  # mean(0.8, 0.6)
    assert c["cost_calls"] == pytest.approx(4 / 4)  # one call per scored unit, capped at 1
    assert c["latency_s_norm"] == pytest.approx(0.0, abs=0.05)  # instant fake

    expected = (
        DEFAULT_WEIGHTS["w1"] * c["acceptance_precision"]
        + DEFAULT_WEIGHTS["w2"] * c["claim_recall"]
        + DEFAULT_WEIGHTS["w3"] * c["promotion_proxy"]
        - DEFAULT_WEIGHTS["w4"] * c["cost_calls"]
        - DEFAULT_WEIGHTS["w5"] * c["latency_s_norm"]
    )
    assert result["score"] == pytest.approx(expected, abs=1e-9)
    assert DEFAULT_WEIGHTS == {"w1": 0.4, "w2": 0.25, "w3": 0.2, "w4": 0.1, "w5": 0.05}


def test_custom_weights_partial_override():
    rows = [doc_row("d1", DOC_TEXT, "included")]
    result = replay_variant(
        variant_by_id("doc_relevant_min_loose"), rows,
        by_doc_id({}),
        weights={"w1": 1.0, "w5": 0.0},  # only these two move; the rest default
    )
    c = result["components"]
    assert (c["tp"], c["fp"], c["fn"]) == (1, 0, 0)
    expected = 1.0 * 1.0 + 0.25 * 1.0 + 0.2 * 0.0 - 0.1 * 1.0 - 0.0 * c["latency_s_norm"]
    assert result["score"] == pytest.approx(expected, abs=1e-9)


def test_promotion_proxy_zero_when_no_g5_rows():
    result = replay_variant(
        variant_by_id("doc_relevant_min_loose"),
        [doc_row("d1", DOC_TEXT, "included")],
        by_doc_id({}),
    )
    assert result["components"]["promotion_proxy"] == 0.0


def test_g4_claim_level_precision_recall():
    """G4 metrics are claim-level against each row's recorded claim verdict."""
    rows = claim_rows(
        {
            0: ("accepted", g4_answer("supports", 0.90)),      # recorded positive
            1: ("reviewed", g4_answer("says_nothing", 0.90)),  # recorded negative
            2: ("dropped", g4_answer("contradicted", 0.90)),   # recorded negative
        }
    )
    rows.append(doc_row("doc-g4", G4_DOC, "included"))
    # replay: claim_0 abstains (sub-floor), claims 1+2 route supports -> accepted
    decision = ok({
        "claim_0": g4_answer("supports", 0.50),
        "claim_1": g4_answer("supports", 0.95),
        "claim_2": g4_answer("supports", 0.95),
    })
    result = replay_variant(variant_by_id("g4_floor_loose"), rows,
                            SpyDecider(lambda s, q: decision))
    c = result["components"]
    assert (c["tp"], c["fp"], c["fn"]) == (0, 2, 1)
    assert c["acceptance_precision"] == 0.0  # nothing accepted was recorded positive
    assert c["claim_recall"] == 0.0
    assert c["cost_calls"] == pytest.approx(1 / 3)  # ONE batched call, three claims


def test_g4_incumbent_reroute_is_zero_call_and_reproduces_recorded():
    rows = claim_rows(
        {
            0: ("accepted", g4_answer("supports", 0.90)),
            1: ("reviewed", g4_answer("says_nothing", 0.90)),
            2: ("dropped", g4_answer("contradicted", 0.90)),
        }
    )
    rows.append(doc_row("doc-g4", G4_DOC, "included"))
    decider = SpyDecider(lambda s, q: ok({}))
    result = replay_variant(variant_by_id("g4_incumbent"), rows, decider)

    assert result["status"] == "complete"
    assert result["calls"] == 0
    assert decider.calls == []  # requires_replay None: re-routed from recorded answers
    assert _outcomes(result) == ["accepted", "reviewed", "dropped"]
    c = result["components"]
    assert (c["tp"], c["fp"], c["fn"]) == (1, 0, 0)
    assert c["acceptance_precision"] == 1.0 and c["claim_recall"] == 1.0


# ----------------------------------------------------- 4th-noul doc variant


def test_doc_fourth_noul_variant_asks_and_routes_the_extra_noul():
    variant = variant_by_id("doc_gate_profile_vocab")
    assert PROFILE_VOCAB_QUESTION_ID in variant["config_delta"]["questions"]

    good = ok(doc_answers(0.9, 0.8, **{PROFILE_VOCAB_QUESTION_ID: {"noul": 0.9}}))
    bad = ok(doc_answers(0.9, 0.8, **{PROFILE_VOCAB_QUESTION_ID: {"noul": 0.10}}))
    decider = by_doc_id({"d-vocab": good, "d-novocab": bad})
    rows = [doc_row("d-vocab", DOC_TEXT, "included"),
            doc_row("d-novocab", DOC_TEXT, "included")]
    result = replay_variant(variant, rows, decider)

    # FOUR questions asked: the three shipped Nouls + the variant's addition
    for call in decider.calls:
        assert set(call["questions"]) == {
            "is_relevant", "contains_signal_evidence", "contains_prompt_injection",
            PROFILE_VOCAB_QUESTION_ID,
        }
    assert _outcomes(result) == ["included", "excluded"]
    assert _rows_of(result)[1]["reason"] == "weak_evidence"


# ------------------------------------------------------------------- budget


def test_budget_exhaustion_mid_variant_is_honest_partial():
    rows = [doc_row(f"d{i}", DOC_TEXT, "included") for i in range(4)]
    decider = by_doc_id({})
    result = replay_variant(
        variant_by_id("doc_relevant_min_loose"), rows, decider, budget_calls=2
    )

    assert result["status"] == "partial"
    assert result["calls"] == 2
    assert len(decider.calls) == 2  # stopped AT the budget, never past it
    assert result["components"]["n_pending"] == 2
    # the score is computed over what WAS replayed — honest, not a crash
    assert result["score"] is not None


def test_zero_budget_partial_and_unbounded_complete():
    rows = [doc_row("d1", DOC_TEXT, "included")]
    decider = by_doc_id({})
    zero = replay_variant(variant_by_id("doc_relevant_min_loose"), rows, decider,
                          budget_calls=0)
    assert zero["status"] == "partial" and zero["calls"] == 0

    plenty = replay_variant(variant_by_id("doc_relevant_min_loose"), rows,
                            by_doc_id({}), budget_calls=99)
    assert plenty["status"] == "complete" and plenty["calls"] == 1


def test_g4_budget_counts_batch_calls():
    rows_a = claim_rows({0: ("accepted", g4_answer("supports", 0.9))}, doc_id="doc-a",
                        batch="batch-a")
    rows_b = claim_rows({0: ("accepted", g4_answer("supports", 0.9))}, doc_id="doc-b",
                        batch="batch-b")
    rows = rows_a + rows_b + [doc_row("doc-a", G4_DOC, "included"),
                              doc_row("doc-b", G4_DOC, "included")]
    decider = SpyDecider(lambda s, q: ok({"claim_0": g4_answer("supports", 0.9)}))
    result = replay_variant(variant_by_id("g4_floor_loose"), rows, decider,
                            budget_calls=1)
    assert result["status"] == "partial"
    assert result["calls"] == 1 and result["components"]["n_pending"] == 1


# ------------------------------------------------------- failures / honesty


def test_decider_failure_scored_as_no_verdict_with_cause(tmp_path):
    """A failed Decision mirrors the gate's on_error posture (doc gate:
    use_deterministic -> include), carries Decision.cause onto the row, and
    never raises past the variant."""
    rows = [doc_row("d-err", DOC_TEXT, "excluded")]
    result = replay_variant(
        variant_by_id("doc_relevant_min_loose"), rows,
        SpyDecider(lambda s, q: failed("HTTPStatusError: 500")),
        out_dir=tmp_path,
    )
    assert result["status"] == "complete"
    row = _rows_of(result)[0]
    assert row["outcome"] == "included"  # deterministic include (fail-open)
    assert row["error"] == "HTTPStatusError: 500"
    # recorded "excluded" (negative) + predicted include -> fp, never a crash
    assert (result["components"]["fp"], result["components"]["fn"]) == (1, 0)


def test_not_applicable_decision_is_fail_open_no_verdict():
    rows = [doc_row("d-null", DOC_TEXT, "included")]
    result = replay_variant(variant_by_id("doc_relevant_min_loose"), rows, NullDecider())
    assert result["status"] == "complete"
    assert result["calls"] == 1  # the decider WAS invoked (the request counts)
    assert _outcomes(result) == ["included"]
    assert result["components"]["tp"] == 1  # deterministic include vs recorded include


def test_g4_failure_drops_the_batch_with_cause():
    rows = claim_rows({0: ("accepted", g4_answer("supports", 0.9))})
    rows.append(doc_row("doc-g4", G4_DOC, "included"))
    result = replay_variant(
        variant_by_id("g4_floor_loose"), rows,
        SpyDecider(lambda s, q: failed("BreakerOpen: 5/5 errors > 0.2")),
    )
    assert result["status"] == "complete"
    row = _rows_of(result)[0]
    assert row["outcome"] == "dropped"  # G4 on_error: drop_llm (fail-closed)
    assert row["error"] == "BreakerOpen: 5/5 errors > 0.2"
    assert result["components"]["fn"] == 1  # recorded positive, replayed drop


def test_crashing_decider_is_collapsed_not_raised():
    rows = [doc_row("d-crash", DOC_TEXT, "included"),
            doc_row("d-ok", DOC_TEXT, "included")]
    result = replay_variant(variant_by_id("doc_relevant_min_loose"), rows,
                            CrashingOnFirst())
    assert result["status"] == "complete"  # never raised
    rows_out = _rows_of(result)
    assert rows_out[0]["outcome"] == "included"
    assert "RuntimeError" in rows_out[0]["error"]
    assert rows_out[1]["outcome"] == "included"


def test_two_arg_decider_tolerated_and_gate_threaded_to_three_arg():
    rows = [doc_row("d1", DOC_TEXT, "included")]
    two = TwoArgSpyDecider(lambda s, q: ok(doc_answers(0.9, 0.8)))
    replay_variant(variant_by_id("doc_relevant_min_loose"), rows, two)
    assert two.calls[0]["gate"] == "<not passed>"  # kwarg withheld, no TypeError

    three = SpyDecider(lambda s, q: ok(doc_answers(0.9, 0.8)))
    replay_variant(variant_by_id("doc_relevant_min_loose"), rows, three)
    assert three.calls[0]["gate"] == "lab_replay_doc_gate"


def test_skipped_rows_are_counted_never_hidden():
    rows = [
        doc_row("d-joined", DOC_TEXT, "included"),
        doc_row("d-unjoined", DOC_TEXT, "included", joined=False),
        doc_row("d-nooutcome", DOC_TEXT, "included"),
    ]
    rows[2].pop("outcome")  # a not-applicable gate row: no verdict signal
    result = replay_variant(variant_by_id("doc_relevant_min_loose"), rows,
                            by_doc_id({}))
    assert result["status"] == "complete"
    c = result["components"]
    assert c["n_scored"] == 1
    assert c["n_skipped"] == 2

    # citation rows without persisted claims: skipped with a counter
    no_claims = [dict(claim_rows({0: ("accepted", g4_answer("supports", 0.9))})[0],
                      claims=None)]
    result = replay_variant(variant_by_id("g4_floor_loose"),
                            no_claims + [doc_row("doc-g4", G4_DOC, "included")],
                            SpyDecider(lambda s, q: ok({})))
    assert result["components"]["n_skipped"] >= 1


def test_empty_corpus_is_honest_empty():
    result = replay_variant(variant_by_id("doc_relevant_min_loose"), [],
                            by_doc_id({}))
    assert result["status"] == "empty"
    assert result["score"] is None
    assert result["calls"] == 0


# ------------------------------------------------------ implementer variants


def test_implementer_variants_are_not_replayable():
    impl = next(v for v in default_variants() if v["stage"] == "implementer")
    decider = SpyDecider(lambda s, q: ok({}))
    result = replay_variant(impl, [doc_row("d1", DOC_TEXT, "included")], decider)

    assert result["status"] == "not_replayable"
    assert result["calls"] == 0
    assert result["score"] is None
    assert decider.calls == []  # metadata-only: no request was ever paid


# ------------------------------------------------------------------ out_dir


def test_out_dir_writes_decisions_shaped_jsonl(tmp_path):
    rows = [doc_row("d1", DOC_TEXT, "included"), doc_row("d2", DOC_TEXT, "excluded")]
    variant = variant_by_id("doc_relevant_min_loose")
    result = replay_variant(variant, rows, by_doc_id({}), out_dir=tmp_path)

    path = tmp_path / f"{variant['id']}.jsonl"
    assert path.exists()
    lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l]
    assert len(lines) == 2
    assert path.read_text(encoding="utf-8").endswith("\n")  # LF-terminated
    for line in lines:
        assert line["gate"] == "lab_replay_doc_gate"
        assert line["variant_id"] == variant["id"]
        assert line["state_hash"]  # the deterministic replay input is identifiable
        assert "outcome" in line and "recorded_outcome" in line
    # ...and the file mirrors the rows the result carries
    assert [l["outcome"] for l in lines] == _outcomes(result)


# ------------------------------------------------------------------ advance


def test_advance_requires_the_epsilon_margin():
    assert advance(0.50, 0.40) is True  # 0.10 >= 0.02
    assert advance(0.42, 0.40) is True  # exactly epsilon
    assert advance(0.41, 0.40) is False  # under epsilon
    assert advance(0.30, 0.40) is False  # worse
    assert advance(0.40, 0.40) is False  # tie is not an advance
    assert advance(0.50, 0.40, epsilon=0.05) is True
    assert advance(0.44, 0.40, epsilon=0.05) is False
    assert advance(None, 0.40) is False  # an unscored variant never advances
    assert advance(0.50, None) is False  # ...nor against an unscored incumbent
    assert advance(None, None) is False
