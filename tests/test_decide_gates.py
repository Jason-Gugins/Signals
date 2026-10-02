"""Tests for the five decide-layer gates (src/decide/gates.py).

G1 plan qualification, G2 posture audit (shadow-only by construction), G3
implementer routing, G4 citation soundness (anchor-window state + lexical
prefilter), G5 need promotion — plus the shared DecideLedger and text
helpers. Fully offline: MockDecider only, no network, no respx.

Semantics pinned here (see gates module docstring for the reasoning):
- Enforce mode applies the verdict subject to floor + on_error; shadow mode
  is inert (returns the deterministic/no-gate outcome) while the row records
  what enforce WOULD do.
- ``agree`` compares the Jev-derived outcome against the deterministic
  baseline action, so in enforce mode ``agree: false + error: null`` means
  the floor rejected content the deterministic path would have kept.
- Not-applicable (NullDecider / unmatched mock) fails safe: LLM content is
  dropped (G1/G4), the deterministic choice is returned (G3/G5).
"""

from __future__ import annotations

import copy
import hashlib
import json

from src.core.textutil import truncate
from src.decide import (
    LEXICAL_OVERLAP_FLOOR,
    Claim,
    DecideLedger,
    MockDecider,
    NullDecider,
    PlanStep,
    anchor_window,
    estimate_tokens,
    gate_citation_batch,
    gate_need_promotion,
    gate_plan_step,
    gate_posture_audit,
    gate_routing,
    lexical_overlap,
    state_hash,
)
from src.decide.shapes import Decision

RUN = "run-gates-test"

# All invariant-5 ledger row keys (gates may add extras: called/outcome/noul/reason).
INVARIANT_KEYS = {
    "gate",
    "boundary",
    "state_hash",
    "answers",
    "deterministic_action",
    "agree",
    "agree_direction",
    "error",
    "latency_ms",
    "raw_tokens",
    "model",
    "run_id",
}


class CountingDecider(MockDecider):
    """MockDecider that records every decide() invocation for inspection."""

    def __init__(self, verdicts: dict[str, Decision]) -> None:
        super().__init__(verdicts)
        self.calls: list[tuple[object, dict]] = []

    @property
    def n_calls(self) -> int:
        return len(self.calls)

    def decide(self, state: str | dict, questions: dict[str, dict]) -> Decision:
        self.calls.append((copy.deepcopy(state), copy.deepcopy(questions)))
        return super().decide(state, questions)


def _noul(qid: str, value: float, tokens: int = 50) -> MockDecider:
    """A MockDecider answering one noul question id with ``value``."""
    return MockDecider(
        {qid: Decision(applies=True, ok=True, answers={qid: {"noul": value}}, raw_tokens=tokens)}
    )


def _error(qid: str) -> MockDecider:
    return MockDecider({qid: Decision(applies=True, ok=False, answers={}, raw_tokens=0)})


def _ok(answers: dict[str, dict], tokens: int = 10) -> Decision:
    """A bare applicable, ok Decision (for CountingDecider construction)."""
    return Decision(applies=True, ok=True, answers=answers, raw_tokens=tokens)


def _fail() -> Decision:
    return Decision(applies=True, ok=False, answers={}, raw_tokens=0)


def _choice(qid: str, value: str, tokens: int = 10) -> MockDecider:
    return MockDecider(
        {
            qid: Decision(
                applies=True, ok=True, answers={qid: {"choice": value}}, raw_tokens=tokens
            )
        }
    )


STEP = PlanStep(
    source_ids=["company_feed"],
    budget_knobs={"article_follow_max": 3},
    acceptance_criteria=["yields >=2 relevant posts"],
)

# A document whose head is boilerplate and whose supporting text is
# mid-document (beyond the ~1200-char half-window): anchor-window state must
# reach past the head. The claim is a verbatim (case/whitespace-insensitive)
# substring of the document — the anchor match is a substring match.
HEAD = "Nav header. Cookie banner. " * 60  # 1620 chars of boilerplate
MID = "The vendor disclosed a breach affecting 2.4 million records in March."
TAIL = "Filler sentence about nothing relevant. " * 150
DOC = HEAD + MID + " " + TAIL

GOOD_CLAIM = "vendor disclosed a breach affecting 2.4 million records"
BAD_CLAIM = "Quantum flux capacitor retro encabulator upgraded unprompted"

NEED = "Needs SOC2 aligned managed detection for a 500 seat fleet"
EVIDENCE = (
    HEAD
    + "Their VP of IT said the team needs SOC2 aligned managed detection for a 500 seat fleet."
    + TAIL
)


# --- helpers: estimate_tokens / state_hash / lexical_overlap / anchor_window ---


def test_estimate_tokens_formula():
    assert estimate_tokens("") == 1  # never zero
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcdefgh") == 2
    assert estimate_tokens("a" * 100) == 25


def test_state_hash_is_sha256_of_sorted_keys_json():
    state = {"b": 1, "a": "x", "nested": {"z": [1, 2], "y": None}}
    expected = hashlib.sha256(
        json.dumps(state, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()
    assert state_hash(state) == expected
    # Key order must not matter (normalized).
    assert state_hash({"a": "x", "b": 1, "nested": {"y": None, "z": [1, 2]}}) == expected


def test_lexical_overlap_full_partial_zero():
    doc = "the vendor disclosed a breach affecting many records"
    assert lexical_overlap("vendor disclosed breach", doc) == 1.0
    assert lexical_overlap("vendor disclosed breach quantum", doc) == 0.75
    assert lexical_overlap("quantum flux capacitor", doc) == 0.0
    assert lexical_overlap("", doc) == 0.0  # empty claim -> 0 via max(1, n) guard


def test_lexical_overlap_floor_constant():
    assert LEXICAL_OVERLAP_FLOOR == 0.15


def test_anchor_window_mid_document_contains_anchor_and_head():
    window = anchor_window(DOC, MID)
    assert MID in window  # the anchor itself
    assert "Nav header" in window  # head excerpt included
    assert "Filler sentence" in window  # context past the head (window is centered)
    assert len(window) < len(DOC)  # bounded, not the whole document


def test_anchor_window_missing_anchor_falls_back_to_head():
    window = anchor_window(DOC, "totally absent anchor text here")
    assert window == truncate(DOC, 400)
    assert MID not in window  # head only — no window around a missing anchor


def test_anchor_window_is_case_and_whitespace_insensitive():
    window = anchor_window(DOC, "  the VENDOR   disclosed a breach ")
    assert MID in window


# --- DecideLedger -------------------------------------------------------------


def test_ledger_row_shape_has_all_invariant_keys():
    ledger = DecideLedger()
    gate_need_promotion(NEED, EVIDENCE, False, _noul("promotion", 0.9), {}, ledger, RUN, "enforce")
    row = ledger.rows[0]
    assert INVARIANT_KEYS <= set(row)
    assert row["gate"] == "need_promotion"
    assert row["boundary"] == "need_promotion"
    assert row["model"] == "MockDecider"
    assert row["run_id"] == RUN
    assert isinstance(row["latency_ms"], float)


def test_ledger_aggregate_counts_mean_noul_and_tokens_per_gate():
    ledger = DecideLedger()
    # G5: accept (agree: det=True, jev yes) with 50 tokens.
    gate_need_promotion(
        NEED, EVIDENCE, True, _noul("promotion", 0.9, tokens=50), {}, ledger, RUN, "enforce"
    )
    # G1: drop to deterministic (noul 0.4).
    gate_plan_step(STEP, "icp", _noul("step", 0.4), {}, ledger, RUN, "enforce")

    agg5 = ledger.aggregate("need_promotion")
    assert agg5["gate"] == "need_promotion"
    assert agg5["counts"] == {"called": 1, "accepted": 1, "dropped": 0, "errored": 0}
    assert agg5["mean_noul"] == 0.9
    assert agg5["input_tokens"] == 50

    agg1 = ledger.aggregate("plan_qualification")
    assert agg1["counts"]["dropped"] == 1
    assert agg1["counts"]["accepted"] == 0
    assert agg1["mean_noul"] == 0.4

    # Unknown gate: zeroed aggregate, not an error.
    empty = ledger.aggregate("no_such_gate")
    assert empty["counts"] == {"called": 0, "accepted": 0, "dropped": 0, "errored": 0}
    assert empty["mean_noul"] is None
    assert empty["input_tokens"] == 0
    assert empty["detail"] == []


def test_ledger_detail_holds_only_dropped_or_disagreed_rows():
    ledger = DecideLedger()
    # accepted + agree (det=True, jev promote) -> NOT in detail
    gate_need_promotion(NEED, EVIDENCE, True, _noul("promotion", 0.9), {}, ledger, RUN, "enforce")
    # dropped below floor (det=True, jev no) -> in detail (dropped AND disagreed)
    gate_need_promotion(NEED, EVIDENCE, True, _noul("promotion", 0.3), {}, ledger, RUN, "enforce")
    # agreed drop (det=False, jev no) -> still in detail: the content IS dropped
    gate_need_promotion(NEED, EVIDENCE, False, _noul("promotion", 0.3), {}, ledger, RUN, "enforce")
    detail = ledger.aggregate("need_promotion")["detail"]
    assert len(detail) == 2
    assert all(row["outcome"] == "dropped" for row in detail)
    assert [row["deterministic_action"] for row in detail] == ["promote", "drop"]


def test_ledger_detail_text_fields_truncated_to_400():
    ledger = DecideLedger()
    ledger.record(
        {
            "gate": "citation_soundness",
            "boundary": "citation_soundness",
            "state_hash": "ab" * 32,
            "answers": None,
            "deterministic_action": "accept",
            "agree": None,
            "agree_direction": None,
            "error": None,
            "latency_ms": 0.0,
            "raw_tokens": 0,
            "model": "MockDecider",
            "run_id": RUN,
            "outcome": "dropped",
            "note": "x" * 1000,
        }
    )
    detail = ledger.aggregate("citation_soundness")["detail"]
    assert len(detail) == 1
    assert len(detail[0]["note"]) == 400


def test_ledger_write_produces_valid_jsonl(tmp_path):
    ledger = DecideLedger()
    gate_need_promotion(NEED, EVIDENCE, True, _noul("promotion", 0.9), {}, ledger, RUN, "enforce")
    gate_plan_step(STEP, "icp", _noul("step", 0.4), {}, ledger, RUN, "enforce")

    path = tmp_path / "nested" / "decisions.jsonl"
    ledger.write(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["gate"] == "need_promotion"
    assert second["gate"] == "plan_qualification"
    assert INVARIANT_KEYS <= set(first) and INVARIANT_KEYS <= set(second)


# --- G1: plan qualification ---------------------------------------------------


def test_plan_step_high_noul_keeps_step():
    ledger = DecideLedger()
    action, decision = gate_plan_step(
        STEP, "icp text", _noul("step", 0.9, tokens=50), {"floor": 0.70}, ledger, RUN, "enforce"
    )
    assert action == "keep"
    assert decision.applies is True and decision.ok is True
    row = ledger.rows[0]
    assert row["gate"] == "plan_qualification"
    assert row["noul"] == 0.9
    assert row["outcome"] == "accepted"
    assert row["called"] is True
    assert row["raw_tokens"] == 50
    assert row["error"] is None


def test_plan_step_low_noul_drops_to_deterministic_default():
    ledger = DecideLedger()
    action, _ = gate_plan_step(STEP, "icp text", _noul("step", 0.4), {}, ledger, RUN, "enforce")
    assert action == "drop_llm"
    row = ledger.rows[0]
    assert row["outcome"] == "dropped"
    assert row["deterministic_action"] == "drop_llm"
    # Dropped override -> aggregate detail.
    assert len(ledger.aggregate("plan_qualification")["detail"]) == 1


def test_plan_step_error_on_error_drop_llm():
    ledger = DecideLedger()
    action, decision = gate_plan_step(STEP, "icp text", _error("step"), {}, ledger, RUN, "enforce")
    assert action == "drop_llm"
    row = ledger.rows[0]
    assert row["error"] is not None
    assert row["outcome"] == "errored"
    assert row["agree"] is False  # on_error fallback overrode the (missing) verdict
    assert row["called"] is True


def test_plan_step_null_decider_takes_deterministic_path():
    ledger = DecideLedger()
    action, decision = gate_plan_step(STEP, "icp text", NullDecider(), {}, ledger, RUN, "enforce")
    assert action == "drop_llm"
    assert decision.applies is False
    row = ledger.rows[0]
    assert row["answers"] is None
    assert row["agree"] is None
    assert row["called"] is False
    agg = ledger.aggregate("plan_qualification")
    assert agg["counts"]["called"] == 0
    assert agg["input_tokens"] == 0


def test_plan_step_shadow_is_inert_and_records_verdict():
    ledger = DecideLedger()
    action, _ = gate_plan_step(STEP, "icp text", _noul("step", 0.9), {}, ledger, RUN, "shadow")
    # Shadow never binds: the LLM step flows; the row records the would-be.
    assert action == "keep"
    row = ledger.rows[0]
    assert row["noul"] == 0.9
    assert row["outcome"] == "accepted"  # would-be outcome
    assert row["deterministic_action"] == "drop_llm"
    assert row["agree"] is False  # jev keep vs deterministic drop_llm
    assert row["called"] is True


def test_plan_step_floor_defaults_and_boundary():
    ledger = DecideLedger()
    # Empty gate_cfg -> module default floor 0.70; exactly at the floor keeps.
    action, _ = gate_plan_step(STEP, "icp text", _noul("step", 0.70), {}, ledger, RUN, "enforce")
    assert action == "keep"
    action, _ = gate_plan_step(STEP, "icp text", _noul("step", 0.69), {}, ledger, RUN, "enforce")
    assert action == "drop_llm"


def test_plan_step_state_includes_icp_and_step_spec():
    decider = CountingDecider({"step": _ok({"step": {"noul": 0.9}}, tokens=1)})
    gate_plan_step(STEP, "ICP: buyers of EDR", decider, {}, DecideLedger(), RUN, "enforce")
    state, questions = decider.calls[0]
    assert state["icp_excerpt"] == "ICP: buyers of EDR"
    assert state["step"]["source_ids"] == ["company_feed"]
    assert state["step"]["budget_knobs"] == {"article_follow_max": 3}
    assert state["step"]["acceptance_criteria"] == ["yields >=2 relevant posts"]
    assert list(questions) == ["step"]
    assert questions["step"]["type"] == "noul"


# --- G2: posture audit (shadow-only by construction) --------------------------


def test_posture_audit_records_verdict_but_never_binds():
    ledger = DecideLedger()
    decider = _choice("posture", "outside_posture", tokens=30)
    decision = gate_posture_audit(
        "company_feed", "fetch 5 article pages", decider, {}, ledger, RUN, "enforce"
    )
    # Informational only: the Decision is returned verbatim; nothing else happens.
    assert decision.applies is True and decision.ok is True
    row = ledger.rows[0]
    assert row["gate"] == "posture_audit"
    assert row["boundary"] == "posture_audit"
    assert row["deterministic_action"] == "proceed_deterministic"
    assert row["answers"] == {"posture": {"choice": "outside_posture"}}
    assert row["called"] is True
    assert row["raw_tokens"] == 30
    assert row.get("outcome") is None  # nothing accepted/dropped — audit only
    assert row["agree"] is False  # jev says outside posture, deterministic path proceeded


def test_posture_audit_agrees_when_within_posture():
    ledger = DecideLedger()
    gate_posture_audit(
        "company_feed",
        "fetch 1 feed",
        _choice("posture", "within_posture"),
        {},
        ledger,
        RUN,
        "shadow",
    )
    row = ledger.rows[0]
    assert row["agree"] is True
    assert row["agree_direction"] == "match"


def test_posture_audit_error_recorded_no_raise():
    ledger = DecideLedger()
    decision = gate_posture_audit("src", "scope", _error("posture"), {}, ledger, RUN, "enforce")
    assert decision.ok is False  # returned verbatim, never raised
    row = ledger.rows[0]
    assert row["error"] is not None
    assert row["agree"] is None


def test_posture_audit_null_decider_records_null_row():
    ledger = DecideLedger()
    decision = gate_posture_audit("src", "scope", NullDecider(), {}, ledger, RUN, "enforce")
    assert decision.applies is False
    row = ledger.rows[0]
    assert row["answers"] is None
    assert row["called"] is False
    assert ledger.aggregate("posture_audit")["counts"]["called"] == 0


# --- G3: implementer routing ---------------------------------------------------


def test_routing_far_above_threshold_deep_without_jev_call():
    # Mock would say "quick" — prove Jev is NOT consulted outside the tie band.
    decider = CountingDecider({"routing": _choice("routing", "quick").verdicts["routing"]})
    ledger = DecideLedger()
    action = gate_routing(4000, decider, {"route_threshold": 2000}, ledger, RUN, "enforce")
    assert action == "deep"
    assert decider.n_calls == 0
    row = ledger.rows[0]
    assert row["called"] is False
    assert row["answers"] is None
    assert row["deterministic_action"] == "deep"


def test_routing_far_below_threshold_quick_without_jev_call():
    decider = CountingDecider({"routing": _choice("routing", "deep").verdicts["routing"]})
    action = gate_routing(500, decider, {}, DecideLedger(), RUN, "enforce")
    assert action == "quick"  # default threshold 2000
    assert decider.n_calls == 0


def test_routing_tie_band_jev_breaks_tie_in_enforce():
    decider = CountingDecider({"routing": _choice("routing", "quick").verdicts["routing"]})
    ledger = DecideLedger()
    # 2100 tokens >= 2000 -> heuristic "deep", but within the tie band: Jev wins.
    action = gate_routing(2100, decider, {"route_threshold": 2000}, ledger, RUN, "enforce")
    assert action == "quick"
    assert decider.n_calls == 1
    row = ledger.rows[0]
    assert row["called"] is True
    assert row["answers"] == {"routing": {"choice": "quick"}}
    assert row["deterministic_action"] == "deep"
    assert row["agree"] is False


def test_routing_tie_band_shadow_returns_heuristic():
    decider = CountingDecider({"routing": _choice("routing", "quick").verdicts["routing"]})
    ledger = DecideLedger()
    action = gate_routing(2100, decider, {"route_threshold": 2000}, ledger, RUN, "shadow")
    assert action == "deep"  # deterministic path; verdict only recorded
    row = ledger.rows[0]
    assert row["called"] is True
    assert row["answers"] == {"routing": {"choice": "quick"}}
    assert row["agree"] is False


def test_routing_tie_band_error_uses_deterministic():
    decider = CountingDecider({"routing": _fail()})
    ledger = DecideLedger()
    action = gate_routing(2100, decider, {"route_threshold": 2000}, ledger, RUN, "enforce")
    assert action == "deep"  # on_error: use_deterministic
    row = ledger.rows[0]
    assert row["error"] is not None
    assert row["called"] is True


def test_routing_tie_band_null_decider_uses_heuristic():
    ledger = DecideLedger()
    action = gate_routing(2100, NullDecider(), {"route_threshold": 2000}, ledger, RUN, "enforce")
    assert action == "deep"
    row = ledger.rows[0]
    assert row["called"] is False
    assert row["agree"] is None


def test_routing_tie_band_edges_are_inclusive():
    decider = CountingDecider({"routing": _ok({"routing": {"choice": "deep"}}, tokens=5)})
    ledger = DecideLedger()
    assert gate_routing(1600, decider, {"route_threshold": 2000}, ledger, RUN, "enforce") == "deep"
    assert gate_routing(2400, decider, {"route_threshold": 2000}, ledger, RUN, "enforce") == "deep"
    assert decider.n_calls == 2  # both edges consulted
    assert gate_routing(2401, decider, {"route_threshold": 2000}, ledger, RUN, "enforce") == "deep"
    assert gate_routing(1599, decider, {"route_threshold": 2000}, ledger, RUN, "enforce") == "quick"
    assert decider.n_calls == 2  # outside band -> no further calls


# --- G4: citation soundness ----------------------------------------------------


def _batch_decider(answers: dict[str, dict], tokens: int = 120) -> MockDecider:
    return MockDecider(
        {"claim": Decision(applies=True, ok=True, answers=answers, raw_tokens=tokens)}
    )


def test_citation_batch_accepts_claim_above_floor():
    ledger = DecideLedger()
    claims = [Claim(GOOD_CLAIM, "doc1")]
    accepted, decision = gate_citation_batch(
        claims, "doc1", DOC, _batch_decider({"claim_0": {"noul": 0.9}}), {}, ledger, RUN, "enforce"
    )
    assert accepted == claims
    assert decision.ok is True
    row = ledger.rows[0]
    assert row["gate"] == "citation_soundness"
    assert row["noul"] == 0.9
    assert row["outcome"] == "accepted"
    assert row["called"] is True
    assert row["raw_tokens"] == 120
    assert row.get("reason") is None


def test_citation_batch_drops_claim_below_floor():
    ledger = DecideLedger()
    accepted, _ = gate_citation_batch(
        [Claim(GOOD_CLAIM, "doc1")],
        "doc1",
        DOC,
        _batch_decider({"claim_0": {"noul": 0.3}}),
        {},
        ledger,
        RUN,
        "enforce",
    )
    assert accepted == []
    row = ledger.rows[0]
    assert row["reason"] == "floor"
    assert row["outcome"] == "dropped"
    assert row["noul"] == 0.3
    assert row["agree"] is False  # floor rejected content the additive path would keep
    assert row["error"] is None
    # Dropped claim lands in the aggregate detail.
    detail = ledger.aggregate("citation_soundness")["detail"]
    assert len(detail) == 1 and detail[0]["reason"] == "floor"


def test_citation_batch_missing_doc_dropped_before_jev():
    for empty in (None, ""):
        ledger = DecideLedger()
        decider = CountingDecider({"claim": _ok({"claim_0": {"noul": 0.9}})})
        accepted, _ = gate_citation_batch(
            [Claim(GOOD_CLAIM, "doc1")], "doc1", empty, decider, {}, ledger, RUN, "enforce"
        )
        assert accepted == []
        assert decider.n_calls == 0  # no Jev call for a missing document
        row = ledger.rows[0]
        assert row["reason"] == "no_document"
        assert row["outcome"] == "dropped"
        assert row["called"] is False


def test_citation_batch_zero_overlap_claim_dropped_by_prefilter():
    ledger = DecideLedger()
    decider = CountingDecider({"claim": _ok({"claim_0": {"noul": 0.9}})})
    accepted, _ = gate_citation_batch(
        [Claim(BAD_CLAIM, "doc1")], "doc1", DOC, decider, {}, ledger, RUN, "enforce"
    )
    assert accepted == []
    assert decider.n_calls == 0  # prefilter catches confabulation before tokens are spent
    row = ledger.rows[0]
    assert row["reason"] == "prefilter"
    assert row["outcome"] == "dropped"


def test_citation_batch_jev_error_drops_all_batch_claims():
    ledger = DecideLedger()
    claims = [Claim(GOOD_CLAIM, "doc1"), Claim("Vendor disclosed breach records in March", "doc1")]
    accepted, decision = gate_citation_batch(
        claims, "doc1", DOC, _error("claim"), {}, ledger, RUN, "enforce"
    )
    assert accepted == []  # additive invariant: degrade to exactly the deterministic dossier
    assert decision.ok is False
    assert len(ledger.rows) == 2
    assert all(r["outcome"] == "errored" for r in ledger.rows)
    assert all(r["error"] for r in ledger.rows)
    agg = ledger.aggregate("citation_soundness")
    assert agg["counts"]["errored"] == 2
    assert agg["counts"]["called"] == 1  # ONE Jev call, attributed to the first row


def test_citation_batch_shadow_returns_all_judged_claims():
    ledger = DecideLedger()
    claims = [Claim(GOOD_CLAIM, "doc1")]
    accepted, _ = gate_citation_batch(
        claims, "doc1", DOC, _batch_decider({"claim_0": {"noul": 0.1}}), {}, ledger, RUN, "shadow"
    )
    assert accepted == claims  # shadow never drops on the Jev verdict...
    row = ledger.rows[0]
    assert row["outcome"] == "dropped"  # ...but records what enforce WOULD do
    assert row["reason"] == "floor"
    assert len(ledger.aggregate("citation_soundness")["detail"]) == 1


def test_citation_batch_prefilter_still_binds_in_shadow():
    # The prefilter and doc-existence checks are DETERMINISTIC machinery: they
    # bind in every mode; only the Jev floor is shadow-inert.
    ledger = DecideLedger()
    decider = CountingDecider({"claim": _ok({"claim_0": {"noul": 0.9}})})
    accepted, _ = gate_citation_batch(
        [Claim(BAD_CLAIM, "doc1")], "doc1", DOC, decider, {}, ledger, RUN, "shadow"
    )
    assert accepted == []
    assert decider.n_calls == 0
    assert ledger.rows[0]["reason"] == "prefilter"


def test_citation_batch_null_decider_fails_closed():
    ledger = DecideLedger()
    accepted, decision = gate_citation_batch(
        [Claim(GOOD_CLAIM, "doc1")], "doc1", DOC, NullDecider(), {}, ledger, RUN, "enforce"
    )
    assert accepted == []  # no Jev -> no unverified LLM claims (fail-closed for LLM content)
    assert decision.applies is False
    row = ledger.rows[0]
    assert row["reason"] == "no_decider"
    assert row["called"] is False
    assert row["agree"] is None


def test_citation_batch_anchor_window_state_reaches_past_head():
    decider = CountingDecider({"claim": _ok({"claim_0": {"noul": 0.9}})})
    gate_citation_batch(
        [Claim(GOOD_CLAIM, "doc1")], "doc1", DOC, decider, {}, DecideLedger(), RUN, "enforce"
    )
    state, questions = decider.calls[0]
    assert state["doc_id"] == "doc1"
    assert "claim_0" in state
    # The window must contain the mid-document support text, not just the head.
    assert MID in state["claim_0"]
    assert "Nav header" in state["claim_0"]  # head excerpt rides along
    assert list(questions) == ["claim_0"]
    assert questions["claim_0"]["type"] == "noul"


def test_citation_batch_one_jev_request_per_document():
    claims = [
        Claim(GOOD_CLAIM, "doc1"),
        Claim("Vendor disclosed breach records in March", "doc1"),
        Claim("Vendor disclosed breach in March", "doc1"),
    ]
    decider = CountingDecider(
        {
            "claim": _ok(
                {
                    "claim_0": {"noul": 0.9},
                    "claim_1": {"noul": 0.8},
                    "claim_2": {"noul": 0.7},
                },
                tokens=120,
            )
        }
    )
    ledger = DecideLedger()
    accepted, _ = gate_citation_batch(claims, "doc1", DOC, decider, {}, ledger, RUN, "enforce")
    assert len(accepted) == 3
    assert decider.n_calls == 1  # ONE batched request per document
    assert len(ledger.rows) == 3
    assert [r["called"] for r in ledger.rows] == [True, False, False]  # call attributed once
    agg = ledger.aggregate("citation_soundness")
    assert agg["counts"]["called"] == 1
    assert agg["counts"]["accepted"] == 3
    assert agg["input_tokens"] == 120  # tokens counted once, not per claim
    assert agg["mean_noul"] == (0.9 + 0.8 + 0.7) / 3


def test_citation_batch_question_ids_index_the_full_claim_list():
    # claims[0] is prefilter-dropped; the surviving claim must still be
    # question claim_1 (stable ids over the FULL original list).
    claims = [Claim(BAD_CLAIM, "doc1"), Claim(GOOD_CLAIM, "doc1")]
    decider = CountingDecider({"claim": _ok({"claim_1": {"noul": 0.9}})})
    ledger = DecideLedger()
    accepted, _ = gate_citation_batch(claims, "doc1", DOC, decider, {}, ledger, RUN, "enforce")
    assert accepted == [claims[1]]
    state, questions = decider.calls[0]
    assert list(questions) == ["claim_1"]
    assert "claim_1" in state and "claim_0" not in state  # only survivors get state fields
    assert len(ledger.rows) == 2  # one prefilter row + one judged row
    assert ledger.rows[0]["reason"] == "prefilter"
    assert ledger.rows[1]["noul"] == 0.9


# --- G5: need promotion ---------------------------------------------------------


def test_need_promotion_above_floor_promotes():
    ledger = DecideLedger()
    promote, noul, decision = gate_need_promotion(
        NEED, EVIDENCE, False, _noul("promotion", 0.9, tokens=30), {}, ledger, RUN, "enforce"
    )
    assert promote is True
    assert noul == 0.9
    assert decision.ok is True
    row = ledger.rows[0]
    assert row["outcome"] == "accepted"
    assert row["deterministic_action"] == "drop"
    assert row["agree"] is False  # jev promote vs deterministic drop
    assert row["agree_direction"] == "jev_yes_det_no"  # directional labels are uniform for G5
    assert row["called"] is True


def test_need_promotion_below_floor_drops_but_probability_stored():
    ledger = DecideLedger()
    promote, noul, _ = gate_need_promotion(
        NEED, EVIDENCE, True, _noul("promotion", 0.3), {}, ledger, RUN, "enforce"
    )
    assert promote is False
    assert noul == 0.3  # the probability is stored regardless of the outcome
    row = ledger.rows[0]
    assert row["noul"] == 0.3
    assert row["outcome"] == "dropped"
    assert row["agree"] is False
    assert row["agree_direction"] == "jev_no_det_yes"  # floor vetoed what det would promote
    assert row["error"] is None
    assert ledger.aggregate("need_promotion")["mean_noul"] == 0.3


def test_need_promotion_error_does_not_promote():
    ledger = DecideLedger()
    promote, noul, decision = gate_need_promotion(
        NEED, EVIDENCE, True, _error("promotion"), {}, ledger, RUN, "enforce"
    )
    assert promote is False  # on_error: drop_llm — the LLM need is not promoted
    assert noul is None
    assert decision.ok is False
    row = ledger.rows[0]
    assert row["error"] is not None
    assert row["outcome"] == "errored"
    assert row["agree"] is False


def test_need_promotion_null_decider_returns_deterministic_action():
    ledger = DecideLedger()
    promote, noul, decision = gate_need_promotion(
        NEED, EVIDENCE, True, NullDecider(), {}, ledger, RUN, "enforce"
    )
    assert promote is True  # deterministic path: the caller's deterministic decision stands
    assert noul is None
    assert decision.applies is False
    row = ledger.rows[0]
    assert row["called"] is False
    assert row["answers"] is None
    assert ledger.aggregate("need_promotion")["counts"]["called"] == 0


def test_need_promotion_shadow_returns_deterministic_action_match():
    ledger = DecideLedger()
    promote, noul, _ = gate_need_promotion(
        NEED, EVIDENCE, True, _noul("promotion", 0.9), {}, ledger, RUN, "shadow"
    )
    assert promote is True  # deterministic action
    assert noul == 0.9  # verdict still measured and returned
    row = ledger.rows[0]
    assert row["agree"] is True
    assert row["agree_direction"] == "match"


def test_need_promotion_shadow_direction_jev_yes_det_no():
    ledger = DecideLedger()
    promote, noul, _ = gate_need_promotion(
        NEED, EVIDENCE, False, _noul("promotion", 0.9), {}, ledger, RUN, "shadow"
    )
    assert promote is False  # deterministic action returned in shadow
    assert noul == 0.9
    row = ledger.rows[0]
    assert row["agree"] is False
    assert row["agree_direction"] == "jev_yes_det_no"  # Jev would add a promotion det lacks


def test_need_promotion_shadow_direction_jev_no_det_yes():
    ledger = DecideLedger()
    promote, _, _ = gate_need_promotion(
        NEED, EVIDENCE, True, _noul("promotion", 0.3), {}, ledger, RUN, "shadow"
    )
    assert promote is True  # deterministic action returned in shadow
    row = ledger.rows[0]
    assert row["agree"] is False
    assert row["agree_direction"] == "jev_no_det_yes"  # Jev veto = recall loss vs det


def test_need_promotion_state_anchor_windows_the_evidence():
    decider = CountingDecider({"promotion": _ok({"promotion": {"noul": 0.9}})})
    gate_need_promotion(NEED, EVIDENCE, False, decider, {}, DecideLedger(), RUN, "enforce")
    state, questions = decider.calls[0]
    assert state["need"] == NEED
    # Anchor window: the support sentence (mid-document, past the head) rides
    # in the state, not just the boilerplate head.
    assert "needs SOC2 aligned managed detection" in state["evidence_excerpt"]
    assert "Nav header" in state["evidence_excerpt"]  # head excerpt rides along
    assert list(questions) == ["promotion"]
    assert questions["promotion"]["type"] == "noul"
