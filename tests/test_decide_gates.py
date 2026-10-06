"""Tests for the five decide-layer gates (src/decide/gates.py).

G1 plan qualification, G2 posture audit (shadow-only by construction), G3
implementer routing, G4 citation soundness (anchor-window state + lexical
prefilter), G5 need promotion — plus the shared DecideLedger and text
helpers. Fully offline: MockDecider only, no network, no respx.

Semantics pinned here (see gates module docstring for the reasoning):
- Enforce mode applies the verdict subject to floor + on_error; shadow mode
  never binds (G3/G5 return the deterministic outcome, G4 applies only its
  deterministic machinery, G1 returns the WOULD-BE enforce action — binding
  is the caller's mode decision) while the row records what enforce WOULD do.
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
from pathlib import Path

import yaml

from src.core.textutil import truncate
from src.decide import (
    AMBIGUOUS,
    CONFIDENT,
    DOC_EVIDENCE_MIN,
    DOC_INJECTION_MAX,
    DOC_RELEVANT_MIN,
    FIRE_THRESHOLD,
    LEXICAL_OVERLAP_FLOOR,
    OUTPUT_ACTION_THRESHOLD,
    OUTPUT_REVIEW_THRESHOLD,
    RAISE_PROB_MIN,
    REVIEW_BELOW,
    SEPARATION_MIN,
    Claim,
    DecideLedger,
    MockDecider,
    NullDecider,
    PlanStep,
    anchor_window,
    classify_claims,
    estimate_tokens,
    extract_claim_amount,
    extract_event_date,
    find_money_spans,
    gate_citation_batch,
    gate_completeness,
    gate_document,
    gate_need_promotion,
    gate_plan_step,
    gate_posture_audit,
    gate_routing,
    lexical_overlap,
    rerank_cap_row,
    separation_ratio,
    state_hash,
)
from src.decide.shapes import Decision
from src.llm.implement import FIVE_FIELDS as DOSSIER_FIELDS
from src.signals.taxonomy import Taxonomy

RUN = "run-gates-test"

REPO_ROOT = Path(__file__).resolve().parents[1]

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


def _choice_ans(label: str, prob: float) -> dict:
    """A choice-shaped answer: top label + its probability (Jev's shape)."""
    return {"choice": label, "probabilities": {label: prob}}


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

# The REAL signal taxonomy (config/signals.yaml): the typing battery's
# criteria MUST be built from this object, never a hardcoded list.
TAX = Taxonomy.load("config/signals.yaml")


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


# --- wave-1: config schema + Claim.quote_span + band constant ------------------


def test_decide_yaml_wave1_keys():
    """decide.yaml gains the wave-1 keys; existing gate entries are unchanged."""
    with open(REPO_ROOT / "config" / "decide.yaml", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    gates = cfg["decider"]["gates"]
    # Band keys NEW: review band = [band_low, floor).
    assert gates["citation_soundness"]["band_low"] == 0.30
    assert gates["need_promotion"]["band_low"] == 0.30
    # Document gate NEW: per-doc screen before implementer spend.
    assert gates["document_gate"] == {
        "enabled": True,
        "relevant_min": 0.45,
        "evidence_min": 0.55,
        "injection_max": 0.70,
        "on_error": "use_deterministic",
    }
    # Output screen NEW (rider): batch-level claim screening on the G4 request.
    assert gates["output_screen"] == {
        "enabled": True,
        "review_threshold": 0.35,
        "action_threshold": 0.70,
        "on_error": "use_deterministic",
    }
    # Unchanged entries stay untouched.
    assert gates["plan_qualification"] == {"enabled": True, "floor": 0.70, "on_error": "drop_llm"}
    assert gates["posture_audit"] == {"enabled": True, "shadow_only": True}
    assert gates["routing"] == {
        "enabled": True,
        "route_threshold": 2000,
        "on_error": "use_deterministic",
    }


def test_claim_quote_span_default_and_roundtrip():
    """Claim.quote_span: None = not provided (span check skipped); provided spans round-trip."""
    assert Claim("t", "d").quote_span is None
    claim = Claim("t", "d", evidence_id="ev-0001", quote_span="some quote")
    assert claim.quote_span == "some quote"
    assert claim.evidence_id == "ev-0001"


def test_band_low_constant():
    from src.decide import gates

    assert gates._BAND_LOW == 0.30


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


def test_anchor_window_folds_curly_quotes():
    """An anchor carrying typographic quotes matches a document rendered the
    same way: the curly-quote fold applies to BOTH sides, so the window
    centers on the anchor instead of silently falling back to head-only."""
    doc = HEAD + "The vendor \u201cdisclosed a breach\u201d in March." + TAIL
    window = anchor_window(doc, "vendor \u201cdisclosed a breach\u201d")
    assert "Nav header" in window  # head excerpt still included
    assert "in March." in window  # text ADJACENT to the anchor: window centered
    assert "Filler sentence" in window  # context past the head, beyond the anchor


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
    assert agg5["counts"] == {
        "called": 1,
        "accepted": 1,
        "dropped": 0,
        "errored": 0,
        "reviewed": 0,
    }
    assert agg5["mean_noul"] == 0.9
    assert agg5["input_tokens"] == 50

    agg1 = ledger.aggregate("plan_qualification")
    assert agg1["counts"]["dropped"] == 1
    assert agg1["counts"]["accepted"] == 0
    assert agg1["mean_noul"] == 0.4

    # Unknown gate: zeroed aggregate, not an error.
    empty = ledger.aggregate("no_such_gate")
    assert empty["counts"] == {
        "called": 0,
        "accepted": 0,
        "dropped": 0,
        "errored": 0,
        "reviewed": 0,
    }
    assert empty["mean_noul"] is None
    assert empty["input_tokens"] == 0
    assert empty["detail"] == []


def test_ledger_detail_holds_dropped_reviewed_or_disagreed_rows():
    ledger = DecideLedger()
    # accepted + agree (det=True, jev promote) -> NOT in detail
    gate_need_promotion(NEED, EVIDENCE, True, _noul("promotion", 0.9), {}, ledger, RUN, "enforce")
    # dropped below band_low (det=True, jev no) -> in detail (dropped AND disagreed)
    gate_need_promotion(NEED, EVIDENCE, True, _noul("promotion", 0.2), {}, ledger, RUN, "enforce")
    # review band (det=True, jev abstain) -> review rows join detail too
    gate_need_promotion(NEED, EVIDENCE, True, _noul("promotion", 0.5), {}, ledger, RUN, "enforce")
    detail = ledger.aggregate("need_promotion")["detail"]
    assert len(detail) == 2
    assert [row["outcome"] for row in detail] == ["dropped", "reviewed"]
    assert all(row["deterministic_action"] == "promote" for row in detail)


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
    raw = path.read_bytes()
    # LF-terminated lines on every platform (write uses newline="\n").
    assert b"\r" not in raw
    lines = raw.decode("utf-8").splitlines()
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


def test_plan_step_shadow_returns_would_be_action_and_records_verdict():
    ledger = DecideLedger()
    action, _ = gate_plan_step(STEP, "icp text", _noul("step", 0.9), {}, ledger, RUN, "shadow")
    # Shadow never binds: the action is the WOULD-BE enforce action (keep,
    # because the verdict meets the floor); the caller applies it only in
    # enforce mode.
    assert action == "keep"
    row = ledger.rows[0]
    assert row["noul"] == 0.9
    assert row["outcome"] == "accepted"  # would-be outcome
    assert row["deterministic_action"] == "drop_llm"
    assert row["agree"] is False  # jev keep vs deterministic drop_llm
    assert row["called"] is True


def test_plan_step_shadow_low_noul_returns_would_be_drop():
    """Shadow does NOT blanket-keep: the returned action is what enforce
    would do (drop below the floor), so shadow artifacts stay honest."""
    ledger = DecideLedger()
    action, _ = gate_plan_step(STEP, "icp text", _noul("step", 0.3), {}, ledger, RUN, "shadow")
    assert action == "drop_llm"
    row = ledger.rows[0]
    assert row["noul"] == 0.3
    assert row["outcome"] == "dropped"  # would-be outcome
    assert row["deterministic_action"] == "drop_llm"
    assert row["agree"] is True  # jev drop agrees with the deterministic default
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
        claims,
        "doc1",
        DOC,
        _batch_decider({"claim_0": _choice_ans("supports", 0.9)}),
        {},
        ledger,
        RUN,
        "enforce",
    )
    assert accepted == claims
    assert decision.ok is True
    row = ledger.rows[0]
    assert row["gate"] == "citation_soundness"
    assert row["noul"] == 0.9  # the top label's probability IS the stored noul
    assert row["outcome"] == "accepted"
    assert row["called"] is True
    assert row["raw_tokens"] == 120
    assert row.get("reason") is None


def test_citation_batch_contradicted_drops_claim():
    ledger = DecideLedger()
    accepted, _ = gate_citation_batch(
        [Claim(GOOD_CLAIM, "doc1")],
        "doc1",
        DOC,
        _batch_decider({"claim_0": _choice_ans("contradicted", 0.9)}),
        {},
        ledger,
        RUN,
        "enforce",
    )
    assert accepted == []
    row = ledger.rows[0]
    assert row["reason"] == "contradicted"
    assert row["outcome"] == "dropped"
    assert row["noul"] == 0.9
    assert row["agree"] is False  # the drop rejected content the additive path would keep
    assert row["error"] is None
    # Dropped claim lands in the aggregate detail.
    detail = ledger.aggregate("citation_soundness")["detail"]
    assert len(detail) == 1 and detail[0]["reason"] == "contradicted"


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
        claims,
        "doc1",
        DOC,
        _batch_decider({"claim_0": _choice_ans("contradicted", 0.9)}),
        {},
        ledger,
        RUN,
        "shadow",
    )
    assert accepted == claims  # shadow never drops on the Jev verdict...
    row = ledger.rows[0]
    assert row["outcome"] == "dropped"  # ...but records what enforce WOULD do
    assert row["reason"] == "contradicted"
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
    decider = CountingDecider({"claim": _ok({"claim_0": _choice_ans("supports", 0.9)})})
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
    assert questions["claim_0"]["type"] == "choice"
    assert set(questions["claim_0"]["criteria"]) == {"supports", "contradicted", "says_nothing"}


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
                    "claim_0": _choice_ans("supports", 0.9),
                    "claim_1": _choice_ans("supports", 0.8),
                    "claim_2": _choice_ans("supports", 0.7),
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
    decider = CountingDecider({"claim": _ok({"claim_1": _choice_ans("supports", 0.9)})})
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


def test_need_promotion_below_band_low_drops_but_probability_stored():
    ledger = DecideLedger()
    promote, noul, _ = gate_need_promotion(
        NEED, EVIDENCE, True, _noul("promotion", 0.2), {}, ledger, RUN, "enforce"
    )
    assert promote is False
    assert noul == 0.2  # the probability is stored regardless of the outcome
    row = ledger.rows[0]
    assert row["noul"] == 0.2
    assert row["outcome"] == "dropped"
    assert row["agree"] is False
    assert row["agree_direction"] == "jev_no_det_yes"  # floor vetoed what det would promote
    assert row["error"] is None
    assert ledger.aggregate("need_promotion")["mean_noul"] == 0.2


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


# --- wave-1 task 2: G4 quote-span check + 3-way Choice, G4/G5 bands, review ----


# A document whose supporting sentence carries typographic quotes and a line
# break — the span check's normalization (curly-quote fold + whitespace
# collapse + casefold) must see through both.
CURLY_DOC = (
    HEAD + "The vendor \u201cdisclosed a breach\u201d affecting\n  2.4 million"
    " records in March." + TAIL
)


def test_g4_fabricated_quote_dropped_zero_jev():
    """A claim whose quote_span is absent from the document (after
    normalization) drops BEFORE any Jev spend — zero decider calls."""
    ledger = DecideLedger()
    decider = CountingDecider({"claim": _ok({"claim_0": _choice_ans("supports", 0.95)})})
    claim = Claim(GOOD_CLAIM, "doc1", quote_span="The vendor paid no ransom whatsoever")
    accepted, _ = gate_citation_batch([claim], "doc1", DOC, decider, {}, ledger, RUN, "enforce")
    assert accepted == []
    assert decider.n_calls == 0  # the fabricated quote never reaches Jev
    row = ledger.rows[0]
    assert row["outcome"] == "dropped"
    assert row["reason"] == "fabricated_quote"
    assert row["called"] is False


def test_g4_quote_found_passes_to_jev():
    """A quote wrapped across a line break (with extra whitespace and curly
    quotes) still matches after normalization — the claim proceeds to Jev."""
    ledger = DecideLedger()
    decider = CountingDecider({"claim": _ok({"claim_0": _choice_ans("supports", 0.9)})})
    claim = Claim(
        GOOD_CLAIM,
        "doc1",
        quote_span="disclosed a breach\u201d  affecting 2.4\nmillion records",
    )
    accepted, _ = gate_citation_batch(
        [claim], "doc1", CURLY_DOC, decider, {}, ledger, RUN, "enforce"
    )
    assert accepted == [claim]
    assert decider.n_calls == 1
    assert ledger.rows[0]["outcome"] == "accepted"


def test_g4_missing_quote_skips_span_check():
    """quote_span None = not provided: the span check is skipped and a good
    claim reaches Jev (no fabricated drop)."""
    ledger = DecideLedger()
    decider = CountingDecider({"claim": _ok({"claim_0": _choice_ans("supports", 0.9)})})
    claim = Claim(GOOD_CLAIM, "doc1")  # quote_span None
    accepted, _ = gate_citation_batch([claim], "doc1", DOC, decider, {}, ledger, RUN, "enforce")
    assert accepted == [claim]
    assert decider.n_calls == 1  # reached the Jev call
    assert ledger.rows[0]["outcome"] == "accepted"


def test_g4_three_way_choice_routing():
    """All four branches: supports (prob >= floor) accepts; contradicted
    drops; says_nothing reviews; label probability < floor reviews."""

    def _run(answer: dict):
        ledger = DecideLedger()
        claim = Claim(GOOD_CLAIM, "doc1")
        accepted, _ = gate_citation_batch(
            [claim], "doc1", DOC, _batch_decider({"claim_0": answer}), {}, ledger, RUN, "enforce"
        )
        return accepted, ledger.rows[0]

    accepted, row = _run(_choice_ans("supports", 0.9))
    assert len(accepted) == 1
    assert row["outcome"] == "accepted"
    assert row.get("reason") is None
    assert row["noul"] == 0.9

    accepted, row = _run(_choice_ans("contradicted", 0.9))
    assert accepted == []
    assert row["outcome"] == "dropped"
    assert row["reason"] == "contradicted"

    accepted, row = _run(_choice_ans("says_nothing", 0.9))
    assert accepted == []  # review: the claim is NOT returned in enforce
    assert row["outcome"] == "reviewed"
    assert row["reason"] == "says_nothing"

    accepted, row = _run(_choice_ans("supports", 0.5))  # prob < floor
    assert accepted == []
    assert row["outcome"] == "reviewed"
    assert row["reason"] == "review_label_prob"


def test_g4_band_on_choice_probability():
    """A supports answer with its probability in [band_low, floor) routes to
    review, not accept (top-prob-or-abstain: prob < floor never accepts)."""
    ledger = DecideLedger()
    accepted, _ = gate_citation_batch(
        [Claim(GOOD_CLAIM, "doc1")],
        "doc1",
        DOC,
        _batch_decider({"claim_0": _choice_ans("supports", 0.45)}),
        {},
        ledger,
        RUN,
        "enforce",
    )
    assert accepted == []
    row = ledger.rows[0]
    assert row["outcome"] == "reviewed"
    assert row["reason"] == "review_label_prob"
    assert row["noul"] == 0.45  # the probability is still stored


def test_g4_case_mismatched_choice_label_falls_back_to_raw_key():
    """A Jev answer echoing the label with its original capitalization
    ({"choice": "Supports", "probabilities": {"Supports": 0.9}}) still routes
    on the probability — no spurious review_label_prob from the lowercased
    label missing in the probabilities map."""
    ledger = DecideLedger()
    accepted, _ = gate_citation_batch(
        [Claim(GOOD_CLAIM, "doc1")],
        "doc1",
        DOC,
        _batch_decider({"claim_0": {"choice": "Supports", "probabilities": {"Supports": 0.9}}}),
        {},
        ledger,
        RUN,
        "enforce",
    )
    assert len(accepted) == 1
    row = ledger.rows[0]
    assert row["outcome"] == "accepted"
    assert row["noul"] == 0.9
    assert row.get("reason") is None


def test_g4_nan_noul_answer_treated_as_no_answer():
    """A NaN noul is not a number: the verdict routes to no_answer (review)
    and the value is NOT counted in the noul samples (a NaN row noul would
    poison mean_noul and make json.dumps emit invalid bare NaN)."""
    ledger = DecideLedger()
    accepted, _ = gate_citation_batch(
        [Claim(GOOD_CLAIM, "doc1")],
        "doc1",
        DOC,
        _batch_decider({"claim_0": {"noul": float("nan")}}),
        {},
        ledger,
        RUN,
        "enforce",
    )
    assert accepted == []
    row = ledger.rows[0]
    assert row["outcome"] == "reviewed"
    assert row["reason"] == "no_answer"
    assert "noul" not in row
    agg = ledger.aggregate("citation_soundness")
    assert agg["mean_noul"] is None  # zero noul samples


def test_g4_legacy_noul_answers_keep_floor_routing():
    """Pre-Choice callers (screen_and_gate_claims until Task 3 migrates) still
    hand the gate noul-shaped verdicts: they route on the hard floor,
    unchanged, and never crash the Choice reader."""
    ledger = DecideLedger()
    accepted, _ = gate_citation_batch(
        [Claim(GOOD_CLAIM, "doc1")],
        "doc1",
        DOC,
        _batch_decider({"claim_0": {"noul": 0.9}}),
        {},
        ledger,
        RUN,
        "enforce",
    )
    assert len(accepted) == 1
    assert ledger.rows[0]["outcome"] == "accepted"

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
    assert ledger.rows[0]["outcome"] == "dropped"
    assert ledger.rows[0]["reason"] == "floor"


def test_g5_band_routing():
    """G5's two-sided band: >= floor promotes; [band_low, floor) reviews (NOT
    promoted, probability stored); < band_low drops."""
    cfg = {"floor": 0.70, "band_low": 0.30}

    ledger = DecideLedger()
    promote, noul, _ = gate_need_promotion(
        NEED, EVIDENCE, False, _noul("promotion", 0.8), cfg, ledger, RUN, "enforce"
    )
    assert promote is True
    assert noul == 0.8
    assert ledger.rows[0]["outcome"] == "accepted"

    ledger = DecideLedger()
    promote, noul, _ = gate_need_promotion(
        NEED, EVIDENCE, False, _noul("promotion", 0.5), cfg, ledger, RUN, "enforce"
    )
    assert promote is False  # review: NOT promoted
    assert noul == 0.5  # the probability is still measured and returned
    row = ledger.rows[0]
    assert row["outcome"] == "reviewed"
    assert row["reason"] == "review_below_floor"
    assert row["noul"] == 0.5

    ledger = DecideLedger()
    promote, noul, _ = gate_need_promotion(
        NEED, EVIDENCE, False, _noul("promotion", 0.1), cfg, ledger, RUN, "enforce"
    )
    assert promote is False
    assert noul == 0.1  # stored regardless
    row = ledger.rows[0]
    assert row["outcome"] == "dropped"
    assert row.get("reason") is None


def test_ledger_counts_reviewed():
    """Aggregate counts gain ``reviewed``; review rows land in detail."""
    ledger = DecideLedger()
    gate_need_promotion(NEED, EVIDENCE, True, _noul("promotion", 0.5), {}, ledger, RUN, "enforce")
    agg = ledger.aggregate("need_promotion")
    assert agg["counts"]["reviewed"] == 1
    assert agg["counts"]["accepted"] == 0
    assert agg["counts"]["dropped"] == 0
    detail = agg["detail"]
    assert len(detail) == 1
    assert detail[0]["outcome"] == "reviewed"
    assert detail[0]["reason"] == "review_below_floor"


def test_span_check_binds_in_shadow():
    """The quote-span check is DETERMINISTIC machinery (like the lexical
    prefilter), so it binds in shadow too — while the Jev Choice verdict
    stays shadow-inert."""
    ledger = DecideLedger()
    fabricated = Claim(GOOD_CLAIM, "doc1", quote_span="The vendor paid no ransom whatsoever")
    judged = Claim("Vendor disclosed breach records in March", "doc1")  # no quote_span
    decider = CountingDecider({"claim": _ok({"claim_1": _choice_ans("contradicted", 0.9)})})
    accepted, _ = gate_citation_batch(
        [fabricated, judged], "doc1", DOC, decider, {}, ledger, RUN, "shadow"
    )
    # The fabricated claim is dropped in shadow BEFORE Jev; the survivor is
    # still Jev-judged (one call) ...
    assert accepted == [judged]
    assert decider.n_calls == 1
    assert ledger.rows[0]["reason"] == "fabricated_quote"
    assert ledger.rows[0]["outcome"] == "dropped"
    # ... and the Choice verdict does NOT bind in shadow: the contradicted
    # claim is returned while its row records the would-be drop.
    assert ledger.rows[1]["outcome"] == "dropped"
    assert ledger.rows[1]["reason"] == "contradicted"


# --- wave-1 task 4: document gate (pre-implementer relevance screen) ----------


def _doc_decider(
    relevant: float,
    evidence: float,
    injection: float,
    tokens: int = 25,
    strength: float | None = None,
):
    """A MockDecider answering the doc gate's THREE noul questions (plus the
    wave-3 ``evidence_strength`` head when ``strength`` is given): the one
    batched Decision's answers map carries every asked question id."""
    answers = {
        "is_relevant": {"noul": relevant},
        "contains_signal_evidence": {"noul": evidence},
        "contains_prompt_injection": {"noul": injection},
    }
    if strength is not None:
        answers["evidence_strength"] = {"noul": strength}
    return MockDecider(
        {
            "is_relevant": Decision(
                applies=True,
                ok=True,
                answers=answers,
                raw_tokens=tokens,
            )
        }
    )


def test_doc_gate_module_threshold_defaults():
    assert DOC_RELEVANT_MIN == 0.45
    assert DOC_EVIDENCE_MIN == 0.55
    assert DOC_INJECTION_MAX == 0.70


def test_doc_gate_injection_above_max_excludes_first():
    """First-match-wins: a planted injection post excludes regardless of how
    relevant/evidential it looks (the cookbook's ranked-#1 attack)."""
    ledger = DecideLedger()
    include, decision, _ = gate_document(
        "doc1",
        DOC,
        "Acme",
        _doc_decider(0.95, 0.95, 0.9),
        {},
        ledger,
        RUN,
        "enforce",
    )
    assert include is False
    assert decision.ok is True
    row = ledger.rows[0]
    assert row["gate"] == "document_gate"
    assert row["boundary"] == "document_gate"
    assert row["outcome"] == "excluded"
    assert row["reason"] == "injection"
    assert row["deterministic_action"] == "include"
    assert row["agree"] is False  # vetoed content the additive path would keep
    assert row["called"] is True
    assert row["raw_tokens"] == 25
    assert row["answers"]["contains_prompt_injection"] == {"noul": 0.9}


def test_doc_gate_irrelevant_excludes():
    ledger = DecideLedger()
    include, _, _ = gate_document(
        "doc1", DOC, "Acme", _doc_decider(0.2, 0.9, 0.0), {}, ledger, RUN, "enforce"
    )
    assert include is False
    row = ledger.rows[0]
    assert row["outcome"] == "excluded"
    assert row["reason"] == "irrelevant"


def test_doc_gate_weak_evidence_excludes():
    """Relevant but evidence-poor: below evidence_min excludes."""
    ledger = DecideLedger()
    include, _, _ = gate_document(
        "doc1", DOC, "Acme", _doc_decider(0.9, 0.3, 0.0), {}, ledger, RUN, "enforce"
    )
    assert include is False
    row = ledger.rows[0]
    assert row["outcome"] == "excluded"
    assert row["reason"] == "weak_evidence"


def test_doc_gate_relevant_evidential_clean_doc_includes():
    ledger = DecideLedger()
    include, _, _ = gate_document(
        "doc1", DOC, "Acme", _doc_decider(0.9, 0.9, 0.0), {}, ledger, RUN, "enforce"
    )
    assert include is True
    row = ledger.rows[0]
    assert row["outcome"] == "included"
    assert row.get("reason") is None
    assert row["agree"] is True  # jev include == deterministic include
    assert row["agree_direction"] == "match"


def test_doc_gate_threshold_boundaries():
    """Strict > for injection, strict < for relevant, >= for evidence."""
    ledger = DecideLedger()
    # injection == injection_max (0.70) does NOT exclude on injection ...
    include, _, _ = gate_document(
        "doc1", DOC, "Acme", _doc_decider(0.9, 0.9, 0.70), {}, ledger, RUN, "enforce"
    )
    assert include is True
    # ... but evidence == evidence_min (0.55) DOES include.
    include, _, _ = gate_document(
        "doc1", DOC, "Acme", _doc_decider(0.9, 0.55, 0.0), {}, ledger, RUN, "enforce"
    )
    assert include is True
    # relevant == relevant_min (0.45) is NOT irrelevant.
    include, _, _ = gate_document(
        "doc1", DOC, "Acme", _doc_decider(0.45, 0.9, 0.0), {}, ledger, RUN, "enforce"
    )
    assert include is True
    assert len(ledger.rows) == 3


def test_doc_gate_jev_error_includes_deterministically():
    """on_error use_deterministic: a Jev error is a deterministic pass-through
    (include) with the error recorded on the row."""
    ledger = DecideLedger()
    include, decision, _ = gate_document(
        "doc1", DOC, "Acme", _error("is_relevant"), {}, ledger, RUN, "enforce"
    )
    assert include is True
    assert decision.ok is False
    row = ledger.rows[0]
    assert row["outcome"] == "included"
    assert row["reason"] == "jev_error"
    assert row["error"] == "jev_error"
    assert row["called"] is True
    assert row["agree"] is False  # enforce on_error fallback convention


def test_doc_gate_null_decider_includes_with_not_applicable_row():
    ledger = DecideLedger()
    include, decision, _ = gate_document(
        "doc1", DOC, "Acme", NullDecider(), {}, ledger, RUN, "enforce"
    )
    assert include is True
    assert decision.applies is False
    row = ledger.rows[0]
    assert row["called"] is False
    assert row["answers"] is None
    assert row["agree"] is None
    assert "outcome" not in row  # not-applicable rows carry no outcome
    agg = ledger.aggregate("document_gate")
    assert agg["counts"]["called"] == 0  # NullDecider rows inflate no counts
    assert agg["input_tokens"] == 0


def test_doc_gate_shadow_screens_but_never_excludes():
    """Shadow never binds: the doc is returned even when the verdict would
    exclude it in enforce — the row records the WOULD-BE exclusion."""
    ledger = DecideLedger()
    include, _, _ = gate_document(
        "doc1", DOC, "Acme", _doc_decider(0.2, 0.9, 0.0), {}, ledger, RUN, "shadow"
    )
    assert include is True
    row = ledger.rows[0]
    assert row["outcome"] == "excluded"  # what enforce WOULD do
    assert row["reason"] == "irrelevant"
    assert row["called"] is True


def test_doc_gate_empty_text_includes_without_jev():
    """Nothing to judge: empty/None text includes deterministically (fail-open
    for empty docs) with a not-applicable row and zero Jev spend."""
    for empty in (None, "", "   "):
        ledger = DecideLedger()
        decider = CountingDecider({"is_relevant": _ok({"is_relevant": {"noul": 0.9}})})
        include, _, _ = gate_document("doc1", empty, "Acme", decider, {}, ledger, RUN, "enforce")
        assert include is True
        assert decider.n_calls == 0
        row = ledger.rows[0]
        assert row["called"] is False
        assert "outcome" not in row


def test_doc_gate_one_request_per_doc_state_and_questions():
    decider = CountingDecider(
        {"is_relevant": _ok(
            {
                "is_relevant": {"noul": 0.9},
                "contains_signal_evidence": {"noul": 0.9},
                "contains_prompt_injection": {"noul": 0.0},
            },
            tokens=25,
        )}
    )
    ledger = DecideLedger()
    gate_document("doc1", DOC, "Acme", decider, {}, ledger, RUN, "enforce")
    assert decider.n_calls == 1  # ONE Jev request per document
    state, questions = decider.calls[0]
    assert state["doc_id"] == "doc1"
    assert state["source"] == "Acme"
    assert "breach" in state["text"]  # the (truncated) document body rides in
    assert list(questions) == [
        "is_relevant",
        "contains_signal_evidence",
        "contains_prompt_injection",
    ]
    assert all(q["type"] == "noul" for q in questions.values())
    assert len(ledger.rows) == 1  # one row per doc


def test_doc_gate_state_text_truncated_to_token_cap():
    long_doc = "Filler sentence about nothing relevant. " * 500  # ~10k tokens
    decider = CountingDecider(
        {"is_relevant": _ok(
            {
                "is_relevant": {"noul": 0.9},
                "contains_signal_evidence": {"noul": 0.9},
                "contains_prompt_injection": {"noul": 0.0},
            }
        )}
    )
    gate_document("doc1", long_doc, "Acme", decider, {}, DecideLedger(), RUN, "enforce")
    state, _questions = decider.calls[0]
    # ~2400 tokens at the house 4-chars/token estimate.
    assert len(state["text"]) <= 2400 * 4
    assert len(state["text"]) < len(long_doc)


def test_doc_gate_thresholds_read_from_gate_cfg():
    """Config overrides beat the module defaults — tightened evidence_min
    excludes a doc the defaults would include."""
    ledger = DecideLedger()
    cfg = {"evidence_min": 0.95}
    include, _, _ = gate_document(
        "doc1", DOC, "Acme", _doc_decider(0.9, 0.9, 0.0), cfg, ledger, RUN, "enforce"
    )
    assert include is False
    assert ledger.rows[0]["reason"] == "weak_evidence"

    # A raised injection_max lets a borderline injection through.
    ledger = DecideLedger()
    include, _, _ = gate_document(
        "doc1", DOC, "Acme", _doc_decider(0.9, 0.9, 0.75), {"injection_max": 0.80},
        ledger, RUN, "enforce",
    )
    assert include is True

    # A lowered relevant_min keeps a borderline-relevant doc.
    ledger = DecideLedger()
    include, _, _ = gate_document(
        "doc1", DOC, "Acme", _doc_decider(0.40, 0.9, 0.0), {"relevant_min": 0.30},
        ledger, RUN, "enforce",
    )
    assert include is True


def test_doc_gate_malformed_verdict_includes_with_error_row():
    """A missing noul (malformed verdict) is no verdict: the deterministic
    baseline (include) stands and the gap is recorded."""
    ledger = DecideLedger()
    partial = MockDecider(
        {
            "is_relevant": Decision(
                applies=True,
                ok=True,
                answers={"is_relevant": {"noul": 0.9}},  # two ids missing
                raw_tokens=5,
            )
        }
    )
    include, _, _ = gate_document("doc1", DOC, "Acme", partial, {}, ledger, RUN, "enforce")
    assert include is True
    row = ledger.rows[0]
    assert row["outcome"] == "included"
    assert row["reason"] == "no_answer"
    assert row["error"] == "no_answer"


# --- wave-3: evidence re-rank head RIDES the document gate ---------------------


def test_doc_gate_rerank_adds_fourth_noul_question():
    """rerank=True: ONE fourth Noul head (evidence_strength) rides the SAME
    batched request — zero marginal requests (the wave-3 design)."""
    decider = CountingDecider(
        {"is_relevant": _ok(
            {
                "is_relevant": {"noul": 0.9},
                "contains_signal_evidence": {"noul": 0.9},
                "contains_prompt_injection": {"noul": 0.0},
                "evidence_strength": {"noul": 0.8},
            },
            tokens=25,
        )}
    )
    ledger = DecideLedger()
    include, decision, strength = gate_document(
        "doc1", DOC, "Acme", decider, {}, ledger, RUN, "enforce", rerank=True
    )
    assert decider.n_calls == 1  # still ONE Jev request per document
    _state, questions = decider.calls[0]
    assert list(questions) == [
        "is_relevant",
        "contains_signal_evidence",
        "contains_prompt_injection",
        "evidence_strength",
    ]
    assert questions["evidence_strength"]["type"] == "noul"
    assert "named action" in questions["evidence_strength"]["instructions"]
    assert include is True
    assert decision.ok is True
    assert strength == 0.8


def test_doc_gate_without_rerank_keeps_the_exact_request_set():
    """rerank=False (the default): the request set is byte-identical to the
    wave-1 shape — the fourth head is NEVER asked and no strength is returned."""
    decider = CountingDecider(
        {"is_relevant": _ok(
            {
                "is_relevant": {"noul": 0.9},
                "contains_signal_evidence": {"noul": 0.9},
                "contains_prompt_injection": {"noul": 0.0},
                "evidence_strength": {"noul": 0.8},
            }
        )}
    )
    ledger = DecideLedger()
    include, _decision, strength = gate_document(
        "doc1", DOC, "Acme", decider, {}, ledger, RUN, "enforce"
    )
    assert decider.n_calls == 1
    _state, questions = decider.calls[0]
    assert list(questions) == [
        "is_relevant",
        "contains_signal_evidence",
        "contains_prompt_injection",
    ]
    assert strength is None
    assert include is True
    assert "evidence_strength" not in ledger.rows[0]  # off-path rows byte-identical


def test_doc_gate_rerank_row_carries_strength_for_ordering_audits():
    """The strength rides BOTH the answers map (the verbatim verdict) and an
    explicit ``evidence_strength`` row field — the ordering audit reads the
    field, not the nested answers."""
    ledger = DecideLedger()
    include, _decision, strength = gate_document(
        "doc1",
        DOC,
        "Acme",
        _doc_decider(0.9, 0.9, 0.0, strength=0.8),
        {},
        ledger,
        RUN,
        "enforce",
        rerank=True,
    )
    assert include is True
    assert strength == 0.8
    row = ledger.rows[0]
    assert row["evidence_strength"] == 0.8
    assert row["answers"]["evidence_strength"] == {"noul": 0.8}


def test_doc_gate_rerank_excluded_doc_still_carries_strength():
    """The strength is returned and recorded even when the routing EXCLUDES the
    doc (injection wins first-match) — the ledger keeps the full verdict."""
    ledger = DecideLedger()
    include, _decision, strength = gate_document(
        "doc1",
        DOC,
        "Acme",
        _doc_decider(0.95, 0.95, 0.9, strength=0.1),
        {},
        ledger,
        RUN,
        "enforce",
        rerank=True,
    )
    assert include is False
    assert strength == 0.1
    row = ledger.rows[0]
    assert row["reason"] == "injection"
    assert row["evidence_strength"] == 0.1


def test_doc_gate_rerank_strength_none_on_jev_error():
    """A Jev error includes deterministically (unchanged) and returns NO
    invented strength — the caller's keep_order posture sorts it last."""
    ledger = DecideLedger()
    include, decision, strength = gate_document(
        "doc1", DOC, "Acme", _error("is_relevant"), {}, ledger, RUN, "enforce", rerank=True
    )
    assert include is True
    assert decision.ok is False
    assert strength is None
    row = ledger.rows[0]
    assert row["error"] == "jev_error"
    assert "evidence_strength" not in row


def test_doc_gate_rerank_strength_none_on_null_decider():
    """NullDecider: not-applicable include, no strength — nothing invented."""
    ledger = DecideLedger()
    include, decision, strength = gate_document(
        "doc1", DOC, "Acme", NullDecider(), {}, ledger, RUN, "enforce", rerank=True
    )
    assert include is True
    assert decision.applies is False
    assert strength is None
    row = ledger.rows[0]
    assert row["called"] is False
    assert "evidence_strength" not in row


def test_doc_gate_rerank_missing_head_returns_none_but_routes_normally():
    """The three gate heads answered, evidence_strength missing: the gate
    routes on its OWN heads (include stands) and the missing re-rank head is a
    None strength — no error, no invented value."""
    ledger = DecideLedger()
    include, _decision, strength = gate_document(
        "doc1", DOC, "Acme", _doc_decider(0.9, 0.9, 0.0), {}, ledger, RUN, "enforce", rerank=True
    )
    assert include is True
    assert strength is None
    assert ledger.rows[0].get("reason") is None
    assert "evidence_strength" not in ledger.rows[0]


def test_doc_gate_rerank_shadow_records_strength_and_never_binds():
    """Shadow: the head rides the request, the row records the value, the
    would-be outcome still rides — ordering/capping are the CALLER's
    enforce-only concern; the gate itself never orders, caps or excludes."""
    ledger = DecideLedger()
    include, _decision, strength = gate_document(
        "doc1",
        DOC,
        "Acme",
        _doc_decider(0.2, 0.9, 0.0, strength=0.4),
        {},
        ledger,
        RUN,
        "shadow",
        rerank=True,
    )
    assert include is True  # shadow never binds
    assert strength == 0.4
    row = ledger.rows[0]
    assert row["outcome"] == "excluded"  # what enforce WOULD do
    assert row["evidence_strength"] == 0.4


def test_doc_gate_rerank_empty_text_no_call_strength_none():
    """Empty text with rerank on: the deterministic include fires BEFORE any
    Jev spend and the strength is None."""
    ledger = DecideLedger()
    decider = CountingDecider({})
    include, _decision, strength = gate_document(
        "doc1", "   ", "Acme", decider, {}, ledger, RUN, "enforce", rerank=True
    )
    assert include is True
    assert strength is None
    assert decider.n_calls == 0


def test_rerank_cap_row_marks_the_document_gate_boundary():
    """The keep_n cap's excluded-by-cap row: SAME boundary and state shape as
    the gate's own rows (state_hash joins the include row), outcome excluded,
    reason rerank_cap, no Jev spend, agree None (no verdict judged HERE)."""
    ledger = DecideLedger()
    decider = _doc_decider(0.9, 0.9, 0.0)
    gate_document("doc1", DOC, "Acme", decider, {}, ledger, RUN, "enforce")
    gate_row = ledger.rows[0]
    cap_row = rerank_cap_row("doc1", DOC, "Acme", decider, RUN)
    ledger.record(cap_row)
    assert cap_row["gate"] == "document_gate"
    assert cap_row["boundary"] == "document_gate"
    assert cap_row["state_hash"] == gate_row["state_hash"]
    assert cap_row["outcome"] == "excluded"
    assert cap_row["reason"] == "rerank_cap"
    assert cap_row["deterministic_action"] == "include"
    assert cap_row["called"] is False
    assert cap_row["agree"] is None
    assert cap_row["raw_tokens"] == 0
    assert cap_row["run_id"] == RUN


# --- wave-1 task 5: output screen rider (batch-level claim screening) ----------

SCREEN_CFG = {"enabled": True, "review_threshold": 0.35, "action_threshold": 0.70}

BATCH_CLAIMS = [
    Claim(GOOD_CLAIM, "doc1"),
    Claim("Vendor disclosed breach records in March", "doc1"),
]


def _screen_decider(
    claim_answers: dict[str, dict],
    wrong_entity: float,
    out_of_excerpt: float,
    tokens: int = 140,
) -> MockDecider:
    """A MockDecider answering ONE batched Decision: the per-claim Choice
    answers AND the two output-screen Nouls coexist in the answers map."""
    answers = dict(claim_answers)
    answers["wrong_entity"] = {"noul": wrong_entity}
    answers["out_of_excerpt"] = {"noul": out_of_excerpt}
    return MockDecider(
        {"claim": Decision(applies=True, ok=True, answers=answers, raw_tokens=tokens)}
    )


def _claim_answers() -> dict[str, dict]:
    return {
        "claim_0": _choice_ans("supports", 0.9),
        "claim_1": _choice_ans("supports", 0.8),
    }


def test_output_screen_threshold_constants():
    assert OUTPUT_REVIEW_THRESHOLD == 0.35
    assert OUTPUT_ACTION_THRESHOLD == 0.70


def test_output_screen_block_drops_batch():
    """wrong_entity >= action_threshold (0.70) drops ALL batch claims with
    reason "wrong_entity" — block wins over everything — and the screen rides
    the EXISTING per-document request (still exactly ONE Jev request)."""
    ledger = DecideLedger()
    decider = CountingDecider(
        {"claim": _ok(_claim_answers() | {"wrong_entity": {"noul": 0.8},
                                         "out_of_excerpt": {"noul": 0.1}})}
    )
    accepted, _ = gate_citation_batch(
        BATCH_CLAIMS, "doc1", DOC, decider, {}, ledger, RUN, "enforce", screen_cfg=SCREEN_CFG
    )
    assert accepted == []
    assert decider.n_calls == 1  # the two Nouls rode the existing request
    _state, questions = decider.calls[0]
    assert "out_of_excerpt" in questions and "wrong_entity" in questions
    assert questions["out_of_excerpt"]["type"] == "noul"
    assert questions["wrong_entity"]["type"] == "noul"
    assert len(ledger.rows) == 2
    assert all(r["outcome"] == "dropped" for r in ledger.rows)
    assert all(r["reason"] == "wrong_entity" for r in ledger.rows)
    # The batch answers are visible in the row's answers map.
    assert ledger.rows[0]["answers"]["wrong_entity"] == {"noul": 0.8}
    assert ledger.rows[0]["answers"]["out_of_excerpt"] == {"noul": 0.1}
    assert ledger.rows[0]["answers"]["claim_0"] == _choice_ans("supports", 0.9)
    agg = ledger.aggregate("citation_soundness")
    assert agg["counts"]["dropped"] == 2
    assert agg["counts"]["called"] == 1  # attributed once, not per claim


def test_output_screen_block_out_of_excerpt():
    """out_of_excerpt >= action_threshold (wrong_entity clean) drops ALL batch
    claims with reason "unfounded_claims"."""
    ledger = DecideLedger()
    accepted, _ = gate_citation_batch(
        BATCH_CLAIMS,
        "doc1",
        DOC,
        _screen_decider(_claim_answers(), wrong_entity=0.1, out_of_excerpt=0.9),
        {},
        ledger,
        RUN,
        "enforce",
        screen_cfg=SCREEN_CFG,
    )
    assert accepted == []
    assert len(ledger.rows) == 2
    assert all(r["outcome"] == "dropped" for r in ledger.rows)
    assert all(r["reason"] == "unfounded_claims" for r in ledger.rows)


def test_output_screen_review_band():
    """A hazard in [review_threshold, action_threshold) holds the batch for
    review: claims NOT returned in enforce, rows outcome "reviewed" with
    reason "output_review" (recorded, never applied — Task 2 semantics)."""
    ledger = DecideLedger()
    decider = _screen_decider(_claim_answers(), wrong_entity=0.5, out_of_excerpt=0.0)
    accepted, _ = gate_citation_batch(
        BATCH_CLAIMS, "doc1", DOC, decider, {}, ledger, RUN, "enforce", screen_cfg=SCREEN_CFG
    )
    assert accepted == []  # review: the batch is held, not applied
    assert len(ledger.rows) == 2
    assert all(r["outcome"] == "reviewed" for r in ledger.rows)
    assert all(r["reason"] == "output_review" for r in ledger.rows)
    assert ledger.rows[0]["noul"] == 0.9  # the per-claim probability still stored
    # Review rows land in the aggregate detail alongside dropped/errored.
    detail = ledger.aggregate("citation_soundness")["detail"]
    assert len(detail) == 2


def test_output_screen_disabled():
    """screen_cfg None (or enabled: false) = the screen never existed: NO
    out_of_excerpt/wrong_entity questions are asked and per-claim verdicts
    route unchanged (today's behavior exactly)."""
    for screen_cfg in (None, {"enabled": False, "review_threshold": 0.35,
                              "action_threshold": 0.70}):
        ledger = DecideLedger()
        decider = CountingDecider({"claim": _ok(_claim_answers())})
        accepted, _ = gate_citation_batch(
            BATCH_CLAIMS, "doc1", DOC, decider, {}, ledger, RUN, "enforce",
            screen_cfg=screen_cfg,
        )
        assert len(accepted) == 2  # per-claim verdicts stand
        _state, questions = decider.calls[0]
        assert "out_of_excerpt" not in questions
        assert "wrong_entity" not in questions
        assert set(questions) == {"claim_0", "claim_1"}


def test_output_screen_shadow():
    """Shadow: the screen NEVER binds — claims are returned exactly as the
    per-claim verdicts dictated while the rows record the would-be screen
    outcome; the two batch Nouls are still asked (they are the input)."""
    ledger = DecideLedger()
    decider = CountingDecider(
        {"claim": _ok(_claim_answers() | {"wrong_entity": {"noul": 0.9},
                                          "out_of_excerpt": {"noul": 0.1}})}
    )
    accepted, _ = gate_citation_batch(
        BATCH_CLAIMS, "doc1", DOC, decider, {}, ledger, RUN, "shadow", screen_cfg=SCREEN_CFG
    )
    assert accepted == BATCH_CLAIMS  # per-claim verdicts stood; screen did not bind
    assert decider.n_calls == 1
    _state, questions = decider.calls[0]
    assert "out_of_excerpt" in questions and "wrong_entity" in questions
    assert len(ledger.rows) == 2
    assert all(r["outcome"] == "dropped" for r in ledger.rows)  # would-be outcome
    assert all(r["reason"] == "wrong_entity" for r in ledger.rows)


def test_output_screen_block_beats_review():
    """Both hazards fire at different levels: the block (wrong_entity >=
    action) wins over the review band (out_of_excerpt in [review, action))."""
    ledger = DecideLedger()
    accepted, _ = gate_citation_batch(
        BATCH_CLAIMS,
        "doc1",
        DOC,
        _screen_decider(_claim_answers(), wrong_entity=0.8, out_of_excerpt=0.5),
        {},
        ledger,
        RUN,
        "enforce",
        screen_cfg=SCREEN_CFG,
    )
    assert accepted == []
    assert all(r["reason"] == "wrong_entity" for r in ledger.rows)


def test_output_screen_wrong_entity_wins():
    """Both hazards block: wrong_entity takes precedence (first-listed rule)
    over out_of_excerpt's "unfounded_claims"."""
    ledger = DecideLedger()
    accepted, _ = gate_citation_batch(
        BATCH_CLAIMS,
        "doc1",
        DOC,
        _screen_decider(_claim_answers(), wrong_entity=0.9, out_of_excerpt=0.9),
        {},
        ledger,
        RUN,
        "enforce",
        screen_cfg=SCREEN_CFG,
    )
    assert accepted == []
    assert all(r["reason"] == "wrong_entity" for r in ledger.rows)


def test_output_screen_jev_error_keeps_existing_drop_all_path():
    """The screen adds NO error branch of its own: a Jev error on the batch
    takes the existing drop-all path (on_error drop_llm semantics) unchanged."""
    ledger = DecideLedger()
    decider = CountingDecider({"claim": _fail()})
    accepted, decision = gate_citation_batch(
        BATCH_CLAIMS, "doc1", DOC, decider, {}, ledger, RUN, "enforce", screen_cfg=SCREEN_CFG
    )
    assert accepted == []
    assert decision.ok is False
    assert decider.n_calls == 1  # the screen questions rode the same (failed) request
    assert len(ledger.rows) == 2
    assert all(r["outcome"] == "errored" for r in ledger.rows)
    assert all(r["error"] == "jev_error" for r in ledger.rows)


# --- wave-2 task 2: completeness-verify cascade (five-fields battery) ---------
#
# The SDE-cascade battery applied to the FIVE dossier fields ONLY: claims are
# NOT re-judged here (G4 already gates them). One batched request per doc,
# one Noul head per field — ``<field>::absence_wrong`` when the extractor
# returned unknown (bad = the document actually states it) and
# ``<field>::grounded`` when it returned text (bad = the text is NOT
# supported — a hallucinated fill G4 never saw as a claim). Max-style gate:
# ANY head above fire_threshold fires; never a mean.


def _fields_fixture(*, unknown: tuple[str, ...] = ()) -> dict:
    """A five-fields result with every field filled from the document except
    the named ones (the bare-"unknown" absence shape the extractor emits)."""
    supported = {
        "operational_need": "evaluating SIEM vendors",
        "buying_window": "a breach last quarter",
        "displacement_risk": "consolidate three monitoring tools into one",
        "expansion_signal": "the platform team plans to consolidate",
        "why_now": "a breach last quarter",
    }
    fields: dict = {}
    for field in DOSSIER_FIELDS:
        if field in unknown:
            fields[field] = {"text": "unknown", "doc_id": "doc1"}
        else:
            fields[field] = {"text": supported[field], "doc_id": "doc1"}
    return fields


def _heads(fields: dict) -> dict[str, str]:
    """field -> the head id the battery asks for it under (the caller-side
    mirror of the present/unknown routing; pins the head-id format)."""
    heads: dict[str, str] = {}
    for field in DOSSIER_FIELDS:
        value = fields.get(field)
        text = value.get("text") if isinstance(value, dict) else None
        present = (
            isinstance(text, str)
            and bool(text.strip())
            and text.strip().casefold() != "unknown"
        )
        heads[field] = f"{field}::{'grounded' if present else 'absence_wrong'}"
    return heads


def _comp_decider(
    fields: dict, high: dict[str, float], *, ok: bool = True, tokens: int = 60
) -> CountingDecider:
    """A decider for the completeness battery: one Decision carrying a noul
    for EVERY head (scripted values win, the rest answer a quiet 0.1)."""
    heads = _heads(fields)
    answers = {
        head: {"noul": high.get(head, 0.1)} for head in heads.values()
    }
    decision = Decision(
        applies=True,
        ok=ok,
        answers=answers if ok else {},
        raw_tokens=tokens if ok else 0,
    )
    return CountingDecider({head: decision for head in heads.values()})


def test_completeness_module_threshold_default():
    assert FIRE_THRESHOLD == 0.70


def test_completeness_absence_wrong_fires_for_unknown_field():
    """The doc supports what the extractor returned unknown for: the
    absence_wrong head fires (bad=TRUE) and the row says escalated."""
    fields = _fields_fixture(unknown=("why_now",))
    heads = _heads(fields)
    ledger = DecideLedger()
    fired, decision = gate_completeness(
        "doc1", DOC, fields, _comp_decider(fields, {heads["why_now"]: 0.95}),
        {}, ledger, RUN, "enforce",
    )
    assert fired == ["why_now"]
    assert decision.ok is True
    row = ledger.rows[0]
    assert row["gate"] == "completeness_verify"
    assert row["boundary"] == "completeness_verify"
    assert row["outcome"] == "escalated"
    assert row["reason"] == "why_now::absence_wrong"
    assert row["deterministic_action"] == "pass"
    assert row["agree"] is False  # the battery added work the baseline lacks
    assert row["agree_direction"] is None
    assert row["called"] is True
    assert row["noul"] == 0.95
    assert row["answers"]["why_now::absence_wrong"] == {"noul": 0.95}
    assert row["error"] is None


def test_completeness_grounded_fires_for_hallucinated_field():
    """A present fill the document does not support: the grounded head fires
    (bad=TRUE — G4 never judged the raw fill, this is its only check) and the
    row says quarantined."""
    fields = _fields_fixture()
    heads = _heads(fields)
    ledger = DecideLedger()
    fired, _ = gate_completeness(
        "doc1", DOC, fields, _comp_decider(fields, {heads["operational_need"]: 0.9}),
        {}, ledger, RUN, "enforce",
    )
    assert fired == ["operational_need"]
    row = ledger.rows[0]
    assert row["outcome"] == "quarantined"
    assert row["reason"] == "operational_need::grounded"


def test_completeness_no_head_fires_passes():
    fields = _fields_fixture()
    ledger = DecideLedger()
    fired, _ = gate_completeness(
        "doc1", DOC, fields, _comp_decider(fields, {}), {}, ledger, RUN, "enforce"
    )
    assert fired == []
    row = ledger.rows[0]
    assert row["outcome"] == "passed"
    assert "reason" not in row  # nothing fired, nothing to name
    assert row["agree"] is True  # verdict == deterministic pass-through
    assert row["agree_direction"] == "match"


def test_completeness_max_aggregate_not_mean():
    """One head at 0.95 plus four quiet heads: the mean (~0.27) would stay
    under the threshold — the MAX routing must fire regardless."""
    fields = _fields_fixture()
    heads = _heads(fields)
    ledger = DecideLedger()
    fired, _ = gate_completeness(
        "doc1", DOC, fields, _comp_decider(fields, {heads["expansion_signal"]: 0.95}),
        {}, ledger, RUN, "enforce",
    )
    assert fired == ["expansion_signal"]
    assert ledger.rows[0]["outcome"] == "quarantined"


def test_completeness_fire_threshold_is_strict_and_configurable():
    """P(wrong) == fire_threshold does NOT fire (strict >); a config override
    moves the edge."""
    fields = _fields_fixture()
    heads = _heads(fields)
    ledger = DecideLedger()
    fired, _ = gate_completeness(
        "doc1", DOC, fields, _comp_decider(fields, {heads["why_now"]: 0.70}),
        {}, ledger, RUN, "enforce",
    )
    assert fired == []  # exactly at the default: no fire

    ledger = DecideLedger()
    fired, _ = gate_completeness(
        "doc1", DOC, fields, _comp_decider(fields, {heads["why_now"]: 0.71}),
        {}, ledger, RUN, "enforce",
    )
    assert fired == ["why_now"]

    ledger = DecideLedger()
    fired, _ = gate_completeness(
        "doc1", DOC, fields, _comp_decider(fields, {heads["why_now"]: 0.55}),
        {"fire_threshold": 0.50}, ledger, RUN, "enforce",
    )
    assert fired == ["why_now"]

    ledger = DecideLedger()
    fired, _ = gate_completeness(
        "doc1", DOC, fields, _comp_decider(fields, {heads["why_now"]: 0.45}),
        {"fire_threshold": 0.50}, ledger, RUN, "enforce",
    )
    assert fired == []


def test_completeness_jev_error_skips():
    """on_error skip: a Jev error quarantines nothing and escalates nothing —
    the deterministic output stands, the row records the error."""
    fields = _fields_fixture()
    ledger = DecideLedger()
    fired, decision = gate_completeness(
        "doc1", DOC, fields, _comp_decider(fields, {}, ok=False), {}, ledger, RUN, "enforce"
    )
    assert fired == []
    assert decision.ok is False
    row = ledger.rows[0]
    assert row["outcome"] == "errored"
    assert row["error"] == "jev_error"
    assert row["called"] is True
    assert row["answers"] is None
    assert row["agree"] is False  # enforce on_error fallback convention
    agg = ledger.aggregate("completeness_verify")
    assert agg["counts"]["errored"] == 1


def test_completeness_null_decider_skips():
    ledger = DecideLedger()
    fired, decision = gate_completeness(
        "doc1", DOC, _fields_fixture(), NullDecider(), {}, ledger, RUN, "enforce"
    )
    assert fired == []
    assert decision.applies is False
    row = ledger.rows[0]
    assert row["called"] is False
    assert row["answers"] is None
    assert "outcome" not in row  # not-applicable rows carry no outcome
    agg = ledger.aggregate("completeness_verify")
    assert agg["counts"]["called"] == 0  # NullDecider rows inflate no counts
    assert agg["input_tokens"] == 0


def test_completeness_shadow_records_but_returns_empty():
    """Shadow never binds: the fired list comes back EMPTY (nothing is
    quarantined or escalated) while the row records the would-be outcome."""
    fields = _fields_fixture(unknown=("why_now",))
    heads = _heads(fields)
    ledger = DecideLedger()
    fired, _ = gate_completeness(
        "doc1", DOC, fields, _comp_decider(fields, {heads["why_now"]: 0.95}),
        {}, ledger, RUN, "shadow",
    )
    assert fired == []  # the caller's behavior must not change in shadow
    row = ledger.rows[0]
    assert row["outcome"] == "escalated"  # what enforce WOULD do
    assert row["reason"] == "why_now::absence_wrong"
    assert row["called"] is True


def test_completeness_one_request_per_doc_state_and_questions():
    fields = _fields_fixture(unknown=("why_now",))
    heads = _heads(fields)
    decider = _comp_decider(fields, {})
    ledger = DecideLedger()
    gate_completeness("doc1", DOC, fields, decider, {}, ledger, RUN, "enforce")
    assert decider.n_calls == 1  # ONE Jev request per document
    state, questions = decider.calls[0]
    assert state["doc_id"] == "doc1"
    assert "breach" in state["text"]  # the (truncated) document body rides in
    assert state["operational_need"] == "evaluating SIEM vendors"  # fills ride in
    assert "why_now" not in state  # unknown fields carry no text to verify
    assert list(questions) == [heads[field] for field in DOSSIER_FIELDS]
    assert all(q["type"] == "noul" for q in questions.values())
    # the true/false criteria are spelled out in the instructions
    assert "returning unknown is wrong" in questions[heads["why_now"]]["instructions"]
    assert "unknown is honest" in questions[heads["why_now"]]["instructions"]
    assert (
        "NOT supported by the document"
        in questions[heads["operational_need"]]["instructions"]
    )
    assert len(ledger.rows) == 1  # one row per doc


def test_completeness_empty_doc_text_skips_without_jev():
    """Nothing to verify against: deterministic skip, no Jev spend."""
    for empty in (None, "", "   "):
        ledger = DecideLedger()
        decider = _comp_decider(_fields_fixture(), {})
        fired, _ = gate_completeness(
            "doc1", empty, _fields_fixture(), decider, {}, ledger, RUN, "enforce"
        )
        assert fired == []
        assert decider.n_calls == 0
        assert ledger.rows[0]["called"] is False
        assert "outcome" not in ledger.rows[0]


def test_completeness_malformed_head_fires_fail_closed():
    """A head without a usable noul is no verdict: fail-closed (the G5
    posture) — that field fires rather than standing unverified."""
    fields = _fields_fixture()
    heads = _heads(fields)
    answers = {
        head: {"noul": 0.1} for head in heads.values() if head != heads["displacement_risk"]
    }
    decider = CountingDecider(
        {
            head: Decision(applies=True, ok=True, answers=answers, raw_tokens=30)
            for head in heads.values()
        }
    )
    ledger = DecideLedger()
    fired, _ = gate_completeness("doc1", DOC, fields, decider, {}, ledger, RUN, "enforce")
    assert fired == ["displacement_risk"]
    assert ledger.rows[0]["outcome"] == "quarantined"


def test_completeness_mixed_fire_quarantine_wins_and_reason_lists_heads():
    """A present field and an unknown field fire together: the fired list
    keeps FIVE_FIELDS order, the row summarizes with quarantine winning (the
    binding outcome) and the reason names every fired head."""
    fields = _fields_fixture(unknown=("why_now",))
    heads = _heads(fields)
    ledger = DecideLedger()
    fired, _ = gate_completeness(
        "doc1", DOC, fields,
        _comp_decider(fields, {heads["displacement_risk"]: 0.9, heads["why_now"]: 0.8}),
        {}, ledger, RUN, "enforce",
    )
    assert fired == ["displacement_risk", "why_now"]
    row = ledger.rows[0]
    assert row["outcome"] == "quarantined"
    assert row["reason"] == "displacement_risk::grounded,why_now::absence_wrong"


# --- taxonomy typing (wave-2): accepted-claim type proposals -------------------
#
# classify_claims asks ONE batched Choice request per doc over the ACCEPTED
# claims the CALLER passes (id type_<i>, criteria = the REAL taxonomy's types
# as compact "key: label (category)" strings). Granularity is IN CODE, no
# second call: top-label probability >= confident (0.90) AND separation
# (top/second) >= 2.0 proposes the type; >= ambiguous (0.60) proposes the
# type's category; below that records only. A near-tie (separation < 2.0)
# forces the category fallback even above confident; an unknown label
# proposes nothing. Shadow records the would-be rows and returns NO
# proposals (enforce-only enrichment).


def _type_ans(key: str, top: float, second: float | None = None, second_key: str = "award") -> dict:
    """A typing answer: the chosen type key, its probability and (when given)
    a runner-up probability — the distribution separation_ratio reads."""
    probs = {key: top}
    if second is not None:
        probs[second_key] = second
    return {"choice": key, "probabilities": probs}


def _typing_decider(answers: dict[str, dict], *, ok: bool = True, tokens: int = 40):
    """A decider for the typing battery: ONE Decision answering every
    type_<i> question (scripted per-claim answers)."""
    decision = Decision(
        applies=True, ok=ok, answers=answers if ok else {}, raw_tokens=tokens if ok else 0
    )
    return CountingDecider({"type": decision})


def _claim() -> Claim:
    return Claim(text=GOOD_CLAIM, doc_id="doc1")


def test_typing_threshold_constants():
    assert CONFIDENT == 0.90
    assert AMBIGUOUS == 0.60
    assert SEPARATION_MIN == 2.0


def test_separation_ratio_unit_table():
    """top/second with second = the best NON-top probability; documented
    degenerate edges: fewer than 2 entries -> 1.0, non-positive top -> 0.0,
    a non-positive runner-up -> 1.0 (no tie evidence to report)."""
    assert separation_ratio({"a": 0.9, "b": 0.3}) == 0.9 / 0.3
    # second = the best runner-up, not the second ENTRY
    assert separation_ratio({"a": 0.4, "b": 0.1, "c": 0.1}) == 0.4 / 0.1
    assert separation_ratio({"a": 0.5, "b": 0.2, "c": 0.3}) == 0.5 / 0.3
    assert separation_ratio({"a": 0.5, "b": 0.5}) == 1.0  # exact tie
    assert separation_ratio({"a": 0.5}) == 1.0  # single entry
    assert separation_ratio({}) == 1.0  # nothing to compare
    assert separation_ratio({"a": 0.0}) == 0.0  # zero top
    assert separation_ratio({"a": 0.0, "b": 0.0}) == 0.0
    assert separation_ratio({"a": 1.0, "b": 0.0}) == 1.0  # no usable runner-up


def test_typing_confidence_granularity_table():
    """The three bands route IN CODE off the top-label probability and the
    separation ratio: >= confident AND well separated -> type; >= ambiguous
    -> category; below -> recorded row only, no proposal."""
    cases = [
        # (top prob, runner-up prob, expected outcome)
        (0.95, 0.30, "proposed_type"),
        (0.90, 0.30, "proposed_type"),  # exactly confident counts (>=)
        (0.70, 0.25, "proposed_category"),
        (0.60, 0.20, "proposed_category"),  # exactly ambiguous counts (>=)
        (0.50, 0.30, "skipped"),
    ]
    for top, second, expected in cases:
        ledger = DecideLedger()
        decider = _typing_decider({"type_0": _type_ans("funding_round", top, second)})
        proposals = classify_claims([_claim()], DOC, TAX, decider, {}, ledger, RUN, "enforce")
        assert decider.n_calls == 1, (top, second)
        assert list(proposals) == ([0] if expected != "skipped" else []), (top, second)
        row = ledger.rows[0]
        assert row["gate"] == "taxonomy_typing"
        assert row["boundary"] == "taxonomy_typing"
        assert row["outcome"] == expected, (top, second)
        assert row["answers"]["type_0"] == _type_ans("funding_round", top, second)
        assert row["noul"] == top
        assert row["separation"] == top / second
        if expected == "proposed_type":
            assert proposals[0] == {
                "proposed_type": "funding_round",
                "confidence": top,
                "separation": top / second,
            }
        elif expected == "proposed_category":
            assert proposals[0] == {
                "proposed_category": TAX.get("funding_round").category,
                "confidence": top,
                "separation": top / second,
            }


def test_typing_low_separation_forces_category_fallback():
    """A near-tie (separation < 2.0) is ambiguity even when the top label is
    above confident: the proposal falls back to the category level and the
    row carries the separation so the ambiguity stays visible."""
    ledger = DecideLedger()
    decider = _typing_decider({"type_0": _type_ans("funding_round", 0.95, 0.60)})
    proposals = classify_claims([_claim()], DOC, TAX, decider, {}, ledger, RUN, "enforce")
    assert proposals[0] == {
        "proposed_category": TAX.get("funding_round").category,
        "confidence": 0.95,
        "separation": 0.95 / 0.60,
    }
    assert ledger.rows[0]["outcome"] == "proposed_category"
    assert ledger.rows[0]["separation"] == 0.95 / 0.60


def test_typing_unknown_label_proposes_nothing():
    """A typo'd / unknown type key: no proposal, the row says skipped with
    reason unknown_label."""
    ledger = DecideLedger()
    decider = _typing_decider({"type_0": _type_ans("funding_roudn_typo", 0.95, 0.30)})
    proposals = classify_claims([_claim()], DOC, TAX, decider, {}, ledger, RUN, "enforce")
    assert proposals == {}
    row = ledger.rows[0]
    assert row["outcome"] == "skipped"
    assert row["reason"] == "unknown_label"


def test_typing_missing_answer_skips():
    """A claim without a usable Choice answer: no proposal, recorded skip
    (the G4 no_answer posture)."""
    ledger = DecideLedger()
    decider = _typing_decider({})
    proposals = classify_claims([_claim()], DOC, TAX, decider, {}, ledger, RUN, "enforce")
    assert proposals == {}
    row = ledger.rows[0]
    assert row["outcome"] == "skipped"
    assert row["reason"] == "no_answer"


def test_typing_thresholds_read_from_gate_cfg():
    """confident/ambiguous/separation_min come from the gate config over the
    module defaults; a bad value falls back to the default (the _threshold
    posture)."""
    ledger = DecideLedger()
    decider = _typing_decider({"type_0": _type_ans("funding_round", 0.55, 0.20)})
    proposals = classify_claims(
        [_claim()], DOC, TAX, decider, {"confident": 0.50}, ledger, RUN, "enforce"
    )
    assert proposals[0]["proposed_type"] == "funding_round"  # 0.55 >= 0.50 now

    ledger = DecideLedger()
    decider = _typing_decider({"type_0": _type_ans("funding_round", 0.95, 0.30)})
    proposals = classify_claims(
        [_claim()], DOC, TAX, decider, {"separation_min": 4.0}, ledger, RUN, "enforce"
    )
    # separation 0.95/0.30 ~ 3.17 < 4.0: the fallback fires despite confidence
    assert proposals[0]["proposed_category"] == TAX.get("funding_round").category

    ledger = DecideLedger()
    decider = _typing_decider({"type_0": _type_ans("funding_round", 0.95, 0.30)})
    proposals = classify_claims(
        [_claim()], DOC, TAX, decider, {"confident": "oops"}, ledger, RUN, "enforce"
    )
    assert proposals[0]["proposed_type"] == "funding_round"  # default 0.90 applied


def test_typing_criteria_built_from_the_real_taxonomy():
    """The per-claim question's criteria are the REAL taxonomy's types as
    compact "key: label (category)" strings — 54 options, a known key
    present, never a hardcoded list."""
    decider = _typing_decider({"type_0": _type_ans("llm_need", 0.95, 0.30)})
    ledger = DecideLedger()
    classify_claims([_claim()], DOC, TAX, decider, {}, ledger, RUN, "enforce")
    _state, questions = decider.calls[0]
    question = questions["type_0"]
    assert question["type"] == "choice"
    assert question["instructions"] == (
        "Which signal type best fits this claim? Answer with the type key."
    )
    expected = [f"{t.key}: {t.label} ({t.category})" for t in TAX.all()]
    assert question["criteria"] == expected
    assert len(question["criteria"]) == 54
    assert any(c.startswith("annual_report_10k:") for c in question["criteria"])


def test_typing_one_request_per_doc_and_ids_index_the_passed_list():
    """ONE Jev request per doc; ids type_<i> index the PASSED claim list
    (stable, mirroring the G4 id scheme); each claim's state field carries
    its anchor window into the document."""
    claims = [_claim(), Claim(text=NEED, doc_id="doc1")]
    decider = _typing_decider(
        {
            "type_0": _type_ans("llm_need", 0.95, 0.30),
            "type_1": _type_ans("award", 0.95, 0.20, second_key="llm_need"),
        }
    )
    ledger = DecideLedger()
    proposals = classify_claims(claims, DOC, TAX, decider, {}, ledger, RUN, "enforce")
    assert decider.n_calls == 1
    state, questions = decider.calls[0]
    assert list(questions) == ["type_0", "type_1"]
    assert set(state) == {"claim_0", "claim_1"}
    assert "breach" in state["claim_0"]  # the anchor window reached past the head
    assert len(ledger.rows) == 2  # ONE row per claim
    assert proposals[0]["proposed_type"] == "llm_need"
    assert proposals[1]["proposed_type"] == "award"  # ids index the passed list
    # the call's cost rides on the FIRST row only (the G4 convention)
    assert ledger.rows[0]["called"] is True
    assert ledger.rows[0]["raw_tokens"] == 40
    assert ledger.rows[1]["called"] is False
    assert ledger.rows[1]["raw_tokens"] == 0


def test_typing_skips_entirely_when_no_claims():
    """No accepted claims: no Jev call, empty dict, one not-applicable row."""
    ledger = DecideLedger()
    decider = _typing_decider({})
    proposals = classify_claims([], DOC, TAX, decider, {}, ledger, RUN, "enforce")
    assert proposals == {}
    assert decider.n_calls == 0
    assert len(ledger.rows) == 1
    assert ledger.rows[0]["called"] is False
    assert "outcome" not in ledger.rows[0]  # not-applicable rows carry no outcome


def test_typing_jev_error_skips_with_error_rows():
    """on_error skip: a Jev error proposes nothing; one errored row per
    claim, the call's cost on the first (the G4 error convention)."""
    claims = [_claim(), Claim(text=NEED, doc_id="doc1")]
    ledger = DecideLedger()
    decider = _typing_decider({}, ok=False)
    proposals = classify_claims(claims, DOC, TAX, decider, {}, ledger, RUN, "enforce")
    assert proposals == {}
    assert len(ledger.rows) == 2
    assert all(row["outcome"] == "errored" and row["error"] == "jev_error" for row in ledger.rows)
    assert ledger.rows[0]["called"] is True
    assert ledger.rows[1]["called"] is False
    agg = ledger.aggregate("taxonomy_typing")
    assert agg["counts"]["errored"] == 2
    assert agg["counts"]["called"] == 1


def test_typing_null_decider_skips():
    """NullDecider: not applicable — empty dict, one not-applicable row that
    inflates no aggregate counts."""
    ledger = DecideLedger()
    proposals = classify_claims(
        [_claim(), Claim(text=NEED, doc_id="doc1")],
        DOC,
        TAX,
        NullDecider(),
        {},
        ledger,
        RUN,
        "enforce",
    )
    assert proposals == {}
    assert len(ledger.rows) == 1
    assert ledger.rows[0]["called"] is False
    assert "outcome" not in ledger.rows[0]
    agg = ledger.aggregate("taxonomy_typing")
    assert agg["counts"]["called"] == 0
    assert agg["input_tokens"] == 0


def test_typing_shadow_records_rows_returns_empty():
    """Shadow never binds: rows record the would-be proposal while the
    return stays EMPTY (proposals are enforce-only enrichment — a clean A/B)."""
    ledger = DecideLedger()
    decider = _typing_decider({"type_0": _type_ans("funding_round", 0.95, 0.30)})
    proposals = classify_claims([_claim()], DOC, TAX, decider, {}, ledger, RUN, "shadow")
    assert proposals == {}
    assert decider.n_calls == 1  # the battery still runs for the audit trail
    row = ledger.rows[0]
    assert row["outcome"] == "proposed_type"  # what enforce WOULD do
    assert row["called"] is True
    assert row["separation"] == 0.95 / 0.30


# --- event dates (wave-2): code-side calendar assembly --------------------------
#
# extract_event_date asks ONE batched Choice request with SEVEN fixed
# questions (mode/month/day/year/day_anchor/weekday/week_offset) and NEVER
# lets the model do calendar math: pure code assembles the date against a
# PINNED reference — the document's fetched_at date. Confidence is the MIN
# over the parts the resolved mode actually used; below review_below (or
# mode=none / unresolvable) routes to review with event_at None — a date is
# never fabricated and fetched_at stays the observed date. Shadow records
# the would-be rows and returns the inert posture; NullDecider, a Jev error
# and an unparseable fetched_at all skip.

#: The pinned reference for every assembly test: 2026-08-15, a SATURDAY
#: (weekday() == 5). Relative-date expectations below are pinned to it.
EVENT_FETCHED = "2026-08-15T00:00:00+00:00"

_DATE_QIDS = ["mode", "month", "day", "year", "day_anchor", "weekday", "week_offset"]


def _date_ans(label: str, conf: float | None = 0.95) -> dict:
    answer: dict = {"choice": label}
    if conf is not None:
        answer["confidence"] = conf
    return answer


def _date_answers(
    mode: str = "absolute",
    month: str = "8",
    day: str = "15",
    year: str = "2026",
    anchor: str = "none",
    weekday: str = "none",
    week_offset: str = "none",
    conf: float | None = 0.95,
) -> dict:
    """A full seven-answer battery script; every answer carries its
    per-question confidence."""
    return {
        "mode": _date_ans(mode, conf),
        "month": _date_ans(month, conf),
        "day": _date_ans(day, conf),
        "year": _date_ans(year, conf),
        "day_anchor": _date_ans(anchor, conf),
        "weekday": _date_ans(weekday, conf),
        "week_offset": _date_ans(week_offset, conf),
    }


def _date_decider(answers: dict, *, ok: bool = True) -> CountingDecider:
    """A decider for the date battery: ONE Decision answers every question
    (keyed under each of the seven ids — the MockDecider matches exactly)."""
    decision = Decision(applies=True, ok=ok, answers=answers if ok else {}, raw_tokens=77)
    return CountingDecider({qid: decision for qid in _DATE_QIDS})


def test_event_date_review_below_constant():
    assert REVIEW_BELOW == 0.60


def test_event_date_absolute_full_date():
    ledger = DecideLedger()
    decider = _date_decider(_date_answers())
    result = extract_event_date("doc1", DOC, EVENT_FETCHED, decider, {}, ledger, RUN, "enforce")
    assert decider.n_calls == 1
    assert result == {
        "event_at": "2026-08-15",
        "confidence": 0.95,
        "needs_review": False,
        "note": "",
    }
    row = ledger.rows[0]
    assert row["gate"] == "event_dates"
    assert row["boundary"] == "event_dates"
    assert row["outcome"] == "extracted"
    assert row["event_at"] == "2026-08-15"  # the assembled date rides the row
    assert row["reason"] == "event_at 2026-08-15"
    assert row["noul"] == 0.95  # the confidence is the row's noul sample
    assert set(row["answers"]) == set(_DATE_QIDS)
    assert row["called"] is True and row["raw_tokens"] == 77


def test_event_date_absolute_missing_year_pinned_and_bumped():
    """A stated-none year is the missing-year convention: the PINNED year,
    bumped +1 only when the assembled date sits MORE than 31 days before the
    pinned reference (exactly 31 stays). The pinned year comes from the
    fetched_at, and the code-supplied year is not a model-used part (its
    low confidence cannot drag the min)."""
    # "August 1st" (no year): 14 days before the pinned date -> pinned year
    ledger = DecideLedger()
    decider = _date_decider(_date_answers(month="8", day="1", year="none"))
    result = extract_event_date("doc1", DOC, EVENT_FETCHED, decider, {}, ledger, RUN, "enforce")
    assert result["event_at"] == "2026-08-01"
    assert result["needs_review"] is False
    # exactly 31 days before is not MORE than 31 -> no bump (July 15th)
    ledger = DecideLedger()
    decider = _date_decider(_date_answers(month="7", day="15", year="none"))
    result = extract_event_date("doc1", DOC, EVENT_FETCHED, decider, {}, ledger, RUN, "enforce")
    assert result["event_at"] == "2026-07-15"
    # "March 10th" (no year): 158 days before -> the NEXT year
    ledger = DecideLedger()
    decider = _date_decider(_date_answers(month="3", day="10", year="none"))
    result = extract_event_date("doc1", DOC, EVENT_FETCHED, decider, {}, ledger, RUN, "enforce")
    assert result["event_at"] == "2027-03-10"
    # the year was resolved in CODE: its 0.10 confidence is not a used part
    answers = _date_answers(month="3", day="10", year="none")
    answers["year"] = _date_ans("none", 0.10)
    ledger = DecideLedger()
    decider = _date_decider(answers)
    result = extract_event_date("doc1", DOC, EVENT_FETCHED, decider, {}, ledger, RUN, "enforce")
    assert result["confidence"] == 0.95


def test_event_date_relative_resolves_against_the_pinned_date():
    """The pinned day (2026-08-15, a Saturday) anchors every relative rule:
    a bare weekday is the next occurrence ON OR AFTER it (the pinned day
    itself counts), and today/tomorrow/day_after are fixed offsets."""
    cases = [
        # (day_anchor, weekday, expected)
        ("weekday", "monday", "2026-08-17"),  # next Monday on or after Saturday
        ("weekday", "saturday", "2026-08-15"),  # the pinned day IS that weekday
        ("today", "none", "2026-08-15"),
        ("tomorrow", "none", "2026-08-16"),
        ("day_after", "none", "2026-08-17"),
        # a bare weekday with no day_anchor answer follows the same rule
        ("none", "friday", "2026-08-21"),
    ]
    for anchor, weekday, expected in cases:
        ledger = DecideLedger()
        decider = _date_decider(_date_answers(mode="relative", anchor=anchor, weekday=weekday))
        result = extract_event_date("doc1", DOC, EVENT_FETCHED, decider, {}, ledger, RUN, "enforce")
        assert result["event_at"] == expected, (anchor, weekday)
        assert result["needs_review"] is False, (anchor, weekday)


def test_event_date_relative_week_offset_uses_calendar_weeks():
    """week_offset overrides the on-or-after rule with calendar weeks: the
    pinned Saturday's week starts Monday 2026-08-10, so "current" Friday is
    08-14 (before the pinned day — that is what this week's means) and
    "next" Friday is 08-21."""
    cases = [("current", "2026-08-14"), ("next", "2026-08-21")]
    for offset, expected in cases:
        ledger = DecideLedger()
        decider = _date_decider(
            _date_answers(mode="relative", anchor="weekday", weekday="friday", week_offset=offset)
        )
        result = extract_event_date("doc1", DOC, EVENT_FETCHED, decider, {}, ledger, RUN, "enforce")
        assert result["event_at"] == expected, offset
        assert result["needs_review"] is False, offset


def test_event_date_relative_without_weekday_needs_review():
    """A relative answer that names no usable weekday cannot resolve: review,
    event_at None, never a fabricated date."""
    for anchor in ("weekday", "none"):
        ledger = DecideLedger()
        decider = _date_decider(_date_answers(mode="relative", anchor=anchor, weekday="none"))
        result = extract_event_date("doc1", DOC, EVENT_FETCHED, decider, {}, ledger, RUN, "enforce")
        assert result["event_at"] is None
        assert result["needs_review"] is True
        assert result["note"] == "relative date without a weekday"
        assert ledger.rows[0]["outcome"] == "reviewed"
        assert ledger.rows[0]["reason"] == "relative date without a weekday"


def test_event_date_absolute_incomplete_needs_review():
    """A missing / none / unusable month or day (and a SILENTLY unanswered
    year — silence is not the stated "no year") routes to review with the
    missing parts named."""
    silent_year = _date_answers()
    del silent_year["year"]
    garbage_month = _date_answers(month="August")  # not an option value
    cases = [
        (_date_answers(month="none"), "absolute date incomplete: month"),
        (_date_answers(day="none"), "absolute date incomplete: day"),
        (silent_year, "absolute date incomplete: year"),
        (garbage_month, "absolute date incomplete: month"),
    ]
    for answers, expected_note in cases:
        ledger = DecideLedger()
        decider = _date_decider(answers)
        result = extract_event_date("doc1", DOC, EVENT_FETCHED, decider, {}, ledger, RUN, "enforce")
        assert result["event_at"] is None, expected_note
        assert result["needs_review"] is True, expected_note
        assert result["note"] == expected_note
        assert ledger.rows[0]["outcome"] == "reviewed"


def test_event_date_year_out_of_range_needs_review():
    """A stated year outside 1900-2050 (the out_of_range escape or a raw
    out-of-range value) routes to review; the month/day confidence still
    counts as used."""
    ledger = DecideLedger()
    decider = _date_decider(_date_answers(year="out_of_range"))
    result = extract_event_date("doc1", DOC, EVENT_FETCHED, decider, {}, ledger, RUN, "enforce")
    assert result == {
        "event_at": None,
        "confidence": 0.95,
        "needs_review": True,
        "note": "year stated but out of range",
    }
    ledger = DecideLedger()
    decider = _date_decider(_date_answers(year="1871"))
    result = extract_event_date("doc1", DOC, EVENT_FETCHED, decider, {}, ledger, RUN, "enforce")
    assert result["note"] == "year stated but out of range"


def test_event_date_impossible_dates_need_review():
    """Calendar-impossible combos route to review with the offending combo
    named: Feb 30, a stated Feb 29 in a non-leap pinned year, a missing-year
    Feb 29 neither the pinned nor the bump year has, and a missing-year date
    whose BUMP lands on a nonexistent calendar day."""
    cases = [
        # (year answer, month, day, fetched_at, expected note)
        ("2026", "2", "30", EVENT_FETCHED, "impossible date 2026-02-30"),
        ("2026", "2", "29", EVENT_FETCHED, "impossible date 2026-02-29"),
        ("none", "2", "29", EVENT_FETCHED, "impossible date 2026-02-29"),
        # pinned 2024-12-15: 2024-02-29 exists but sits >31 days past, and
        # the bump year 2025 has no Feb 29 — the bump is what failed
        (
            "none",
            "2",
            "29",
            "2024-12-15T00:00:00+00:00",
            "impossible date 2025-02-29",
        ),
    ]
    for year, month, day, fetched, expected_note in cases:
        ledger = DecideLedger()
        decider = _date_decider(_date_answers(month=month, day=day, year=year))
        result = extract_event_date("doc1", DOC, fetched, decider, {}, ledger, RUN, "enforce")
        assert result["event_at"] is None, expected_note
        assert result["needs_review"] is True, expected_note
        assert result["note"] == expected_note, expected_note
        assert ledger.rows[0]["outcome"] == "reviewed"


def test_event_date_mode_none_and_missing_mode_need_review():
    """mode=none (and no mode answer at all): "no date stated" — the pinned
    fetched_at stays the observed date, a date is never fabricated."""
    ledger = DecideLedger()
    decider = _date_decider(_date_answers(mode="none"))
    result = extract_event_date("doc1", DOC, EVENT_FETCHED, decider, {}, ledger, RUN, "enforce")
    assert result == {
        "event_at": None,
        "confidence": 0.95,
        "needs_review": True,
        "note": "no date stated",
    }
    assert ledger.rows[0]["outcome"] == "reviewed"
    # no mode ANSWER at all: same routing, no confidence to report
    answers = _date_answers()
    del answers["mode"]
    ledger = DecideLedger()
    decider = _date_decider(answers)
    result = extract_event_date("doc1", DOC, EVENT_FETCHED, decider, {}, ledger, RUN, "enforce")
    assert result["event_at"] is None
    assert result["needs_review"] is True
    assert result["note"] == "no date stated"
    assert result["confidence"] is None


def test_event_date_min_confidence_over_used_parts():
    """Confidence is the MIN over the parts the resolved mode actually used:
    a low-confidence month drags an absolute date into review; the month and
    year answers are NOT read for a relative date and cannot drag it; no
    confidence anywhere routes to review honestly; the house probabilities
    map works as a confidence fallback."""
    # a low-confidence month drags the min below review_below
    answers = _date_answers()
    answers["month"] = _date_ans("8", 0.40)
    ledger = DecideLedger()
    decider = _date_decider(answers)
    result = extract_event_date("doc1", DOC, EVENT_FETCHED, decider, {}, ledger, RUN, "enforce")
    assert result["confidence"] == 0.40
    assert result["needs_review"] is True and result["event_at"] is None
    assert result["note"] == "confidence 0.40 below review_below 0.60"
    assert ledger.rows[0]["outcome"] == "reviewed"
    assert ledger.rows[0]["noul"] == 0.40
    # parts NOT read for the resolved mode are excluded from the min
    answers = _date_answers(mode="relative", anchor="weekday", weekday="monday")
    answers["month"] = _date_ans("8", 0.10)
    answers["year"] = _date_ans("2026", 0.10)
    ledger = DecideLedger()
    decider = _date_decider(answers)
    result = extract_event_date("doc1", DOC, EVENT_FETCHED, decider, {}, ledger, RUN, "enforce")
    assert result["confidence"] == 0.95
    assert result["event_at"] == "2026-08-17"
    # no used part carries a confidence: review, honestly
    ledger = DecideLedger()
    decider = _date_decider(_date_answers(conf=None))
    result = extract_event_date("doc1", DOC, EVENT_FETCHED, decider, {}, ledger, RUN, "enforce")
    assert result["confidence"] is None and result["needs_review"] is True
    assert result["note"] == "no confidence reported"
    # the probabilities-map convention (the house _choice_prob fallback)
    answers = _date_answers(conf=None)
    answers["month"] = {"choice": "8", "probabilities": {"8": 0.80}}
    ledger = DecideLedger()
    decider = _date_decider(answers)
    result = extract_event_date("doc1", DOC, EVENT_FETCHED, decider, {}, ledger, RUN, "enforce")
    assert result["confidence"] == 0.80


def test_event_date_review_below_routing_and_config():
    """review_below routes in code: exactly the threshold is NOT below it;
    the gate config overrides the module default; a bad config value falls
    back to the default (the _threshold posture)."""
    ledger = DecideLedger()
    decider = _date_decider(_date_answers(conf=0.60))
    result = extract_event_date("doc1", DOC, EVENT_FETCHED, decider, {}, ledger, RUN, "enforce")
    assert result["needs_review"] is False and result["event_at"] == "2026-08-15"
    # config raises the bar: 0.95 now reviews
    ledger = DecideLedger()
    decider = _date_decider(_date_answers(conf=0.95))
    result = extract_event_date(
        "doc1", DOC, EVENT_FETCHED, decider, {"review_below": 0.99}, ledger, RUN, "enforce"
    )
    assert result["needs_review"] is True and result["confidence"] == 0.95
    # a bad config value falls back to the default 0.60
    ledger = DecideLedger()
    decider = _date_decider(_date_answers(conf=0.59))
    result = extract_event_date(
        "doc1", DOC, EVENT_FETCHED, decider, {"review_below": "oops"}, ledger, RUN, "enforce"
    )
    assert result["needs_review"] is True and result["confidence"] == 0.59
    ledger = DecideLedger()
    decider = _date_decider(_date_answers(conf=0.60))
    result = extract_event_date(
        "doc1", DOC, EVENT_FETCHED, decider, {"review_below": "oops"}, ledger, RUN, "enforce"
    )
    assert result["needs_review"] is False


def test_event_date_one_request_seven_questions():
    """ONE Jev request carrying exactly the seven fixed Choice questions;
    the state carries only the document (NO fetched-date hint — the model
    never does calendar math); every criteria dict ships the specified
    option range COMPACT."""
    decider = _date_decider(_date_answers())
    ledger = DecideLedger()
    extract_event_date("doc1", DOC, EVENT_FETCHED, decider, {}, ledger, RUN, "enforce")
    assert decider.n_calls == 1
    assert len(ledger.rows) == 1  # ONE row per doc
    state, questions = decider.calls[0]
    assert list(questions) == _DATE_QIDS
    assert set(state) == {"doc_id", "text"}
    assert state["doc_id"] == "doc1"
    for qid in _DATE_QIDS:
        assert questions[qid]["type"] == "choice"
        assert isinstance(questions[qid]["criteria"], dict)
        assert questions[qid]["criteria"]
    assert list(questions["mode"]["criteria"]) == ["absolute", "relative", "none"]
    assert set(questions["month"]["criteria"]) == {str(m) for m in range(1, 13)} | {"none"}
    assert set(questions["day"]["criteria"]) == {str(d) for d in range(1, 32)} | {"none"}
    year_keys = set(questions["year"]["criteria"])
    assert {"1900", "2050", "none", "out_of_range"} <= year_keys
    assert len(year_keys) == 153  # 151 in-range years + the two escapes
    assert set(questions["day_anchor"]["criteria"]) == {
        "today",
        "tomorrow",
        "day_after",
        "weekday",
        "none",
    }
    assert set(questions["weekday"]["criteria"]) == {
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
        "none",
    }
    assert set(questions["week_offset"]["criteria"]) == {"next", "current", "none"}


def test_event_date_unparseable_fetched_at_skips_without_jev():
    """An unparseable/missing fetched_at fails honest WITHOUT any Jev call:
    every assembly rule anchors on the pinned reference, so an unusable one
    means no extraction — never a fabricated date."""
    for bad in (None, "", "not-a-date"):
        ledger = DecideLedger()
        decider = _date_decider(_date_answers())
        result = extract_event_date("doc1", DOC, bad, decider, {}, ledger, RUN, "enforce")
        assert decider.n_calls == 0, bad
        assert result == {
            "event_at": None,
            "confidence": None,
            "needs_review": False,
            "note": "unparseable fetched_at",
        }
        assert len(ledger.rows) == 1
        assert ledger.rows[0]["called"] is False


def test_event_date_null_decider_skips():
    """NullDecider: not applicable — the deterministic behavior (no event
    dates) stands, one not-applicable row that inflates no counts."""
    ledger = DecideLedger()
    result = extract_event_date(
        "doc1", DOC, EVENT_FETCHED, NullDecider(), {}, ledger, RUN, "enforce"
    )
    assert result == {
        "event_at": None,
        "confidence": None,
        "needs_review": False,
        "note": "unavailable",
    }
    assert len(ledger.rows) == 1
    assert ledger.rows[0]["called"] is False
    assert "outcome" not in ledger.rows[0]
    agg = ledger.aggregate("event_dates")
    assert agg["counts"]["called"] == 0
    assert agg["input_tokens"] == 0


def test_event_date_jev_error_skips():
    """on_error: skip — a Jev error leaves the deterministic behavior
    untouched ("unavailable") with the error recorded on the row."""
    ledger = DecideLedger()
    decider = _date_decider({}, ok=False)
    result = extract_event_date("doc1", DOC, EVENT_FETCHED, decider, {}, ledger, RUN, "enforce")
    assert result == {
        "event_at": None,
        "confidence": None,
        "needs_review": False,
        "note": "unavailable",
    }
    assert decider.n_calls == 1
    row = ledger.rows[0]
    assert row["outcome"] == "errored"
    assert row["error"] == "jev_error"
    assert ledger.aggregate("event_dates")["counts"]["errored"] == 1


def test_event_date_shadow_records_rows_returns_shadow():
    """Shadow never binds: the battery still runs (rows record what enforce
    WOULD do, assembled date included) while the return stays the inert
    posture — enforce-only enrichment, a clean A/B."""
    ledger = DecideLedger()
    decider = _date_decider(_date_answers())
    result = extract_event_date("doc1", DOC, EVENT_FETCHED, decider, {}, ledger, RUN, "shadow")
    assert result == {
        "event_at": None,
        "confidence": None,
        "needs_review": False,
        "note": "shadow",
    }
    assert decider.n_calls == 1
    row = ledger.rows[0]
    assert row["outcome"] == "extracted"  # what enforce WOULD do
    assert row["event_at"] == "2026-08-15"


# --- value extraction (wave-2): pre-parsed amounts in claims --------------------
#
# find_money_spans is PURE deterministic regex (NO Jev): recall-tuned money
# patterns anchored on a currency token, spans returned verbatim, deduped in
# document order. extract_claim_amount asks ONE small request per
# money-bearing claim: a Choice amount_pick whose options ARE the verbatim
# spans + a "none" escape, and a Noul amount_is_raise; code copies the picked
# span VERBATIM from the spans list and normalizes (regex proposes -> model
# selects -> code normalizes). "none" or raise probability < RAISE_PROB_MIN
# (0.5) means no enrichment (honest absence). Shadow: rows only, return None.
# on_error skip. NullDecider: one not-applicable row. Claims without spans:
# no request, no row, nothing logged.


MONEY_CLAIM = "Acme raised $12.5M in Series B funding last quarter."
VALUATION_CLAIM = "Acme raised its round at a $800M valuation last week."


def _amount_decider(
    pick: str | None = "$12.5M",
    raise_prob: float | None = 0.9,
    *,
    ok: bool = True,
    tokens: int = 30,
) -> CountingDecider:
    """A decider for the value-extraction request: ONE Decision answering
    BOTH questions (MockDecider's 'amount' prefix serves amount_pick and
    amount_is_raise with one verdict, the shape of the real battery)."""
    answers: dict[str, dict] = {}
    if pick is not None:
        answers["amount_pick"] = {"choice": pick, "probabilities": {pick: 0.9}}
    if raise_prob is not None:
        answers["amount_is_raise"] = {"noul": raise_prob}
    decision = Decision(
        applies=True, ok=ok, answers=answers if ok else {}, raw_tokens=tokens if ok else 0
    )
    return CountingDecider({"amount": decision})


def test_raise_prob_min_constant():
    assert RAISE_PROB_MIN == 0.5


def test_find_money_spans_suffix_variants():
    """$N[.N] with a case-insensitive single-letter multiplier suffix."""
    assert find_money_spans("raised $12.5M in May") == ["$12.5M"]
    assert find_money_spans("raised $12.5m in May") == ["$12.5m"]
    assert find_money_spans("budget of $12.5B approved") == ["$12.5B"]
    assert find_money_spans("budget of $12.5b approved") == ["$12.5b"]
    assert find_money_spans("grant of $12.5K landed") == ["$12.5K"]
    assert find_money_spans("grant of $12.5k landed") == ["$12.5k"]
    assert find_money_spans("seed of $3M") == ["$3M"]


def test_find_money_spans_comma_and_plain():
    """Comma-grouped and plain dollar figures without a multiplier."""
    assert find_money_spans("contract worth $12,500,000 signed") == ["$12,500,000"]
    assert find_money_spans("raised $125000 in angels") == ["$125000"]
    assert find_money_spans("fees of $125,000.50 agreed") == ["$125,000.50"]


def test_find_money_spans_symbol_word_multiplier():
    """Currency symbol + word multiplier (million/billion/thousand)."""
    assert find_money_spans("raised €3 million in grants") == ["€3 million"]
    assert find_money_spans("deal worth €3.2 billion closed") == ["€3.2 billion"]
    assert find_money_spans("spent £1.5m on ads") == ["£1.5m"]
    assert find_money_spans("a 2 thousand dollar stipend") == []


def test_find_money_spans_iso_prefix():
    """ISO currency code + number (+ word or suffix multiplier)."""
    assert find_money_spans("raised USD 1.5B in debt") == ["USD 1.5B"]
    assert find_money_spans("contract USD 1,250,000 awarded") == ["USD 1,250,000"]
    assert find_money_spans("EUR 3 million tender won") == ["EUR 3 million"]
    assert find_money_spans("GBP 2 million grant awarded") == ["GBP 2 million"]


def test_find_money_spans_word_dollars():
    """Bare number + word multiplier + 'dollars' (no symbol, no ISO code)."""
    assert find_money_spans("raised 1.5 million dollars in angels") == [
        "1.5 million dollars"
    ]
    assert find_money_spans("the project costs 3 billion dollars") == [
        "3 billion dollars"
    ]


def test_find_money_spans_no_false_negative_iso_word():
    """The cookbook's regression: a word multiplier after an ISO prefix."""
    assert find_money_spans("raised USD 1.5 billion") == ["USD 1.5 billion"]


def test_find_money_spans_no_false_positives():
    """Every pattern anchors on a currency token — bare numbers, counts and
    empty/None text match nothing."""
    assert find_money_spans("12 employees and 3 patents filed") == []
    assert find_money_spans("no money mentioned here at all") == []
    assert find_money_spans("") == []
    assert find_money_spans(None) == []


def test_find_money_spans_dedupe_and_document_order():
    """Repeats collapse to the first occurrence; distinct spans keep
    document order."""
    text = "raised $5M in 2024, $5M more in 2025, then €2 million in 2026"
    assert find_money_spans(text) == ["$5M", "€2 million"]
    assert find_money_spans("€2 million first, $5M second, $5M again") == [
        "€2 million",
        "$5M",
    ]


def test_find_money_spans_verbatim_and_overlap_collapse():
    """Spans are substrings of the ORIGINAL text (case preserved); a symbol
    span and a word-dollars span covering the same figure collapse to the
    earlier one — one candidate, not two competing options."""
    text = "a breach affecting 2.4 million records; Acme raised $12.5M."
    spans = find_money_spans(text)
    assert spans == ["$12.5M"]
    for span in spans:
        assert span in text
    assert find_money_spans("raised $3 million dollars") == ["$3 million"]


def test_claim_amount_extracted_row_and_result():
    """Enforce, raise figure: ONE request carrying claim + candidate spans,
    both questions; the verbatim span normalizes in code; the row records
    the answers, the raise probability and the amount."""
    ledger = DecideLedger()
    decider = _amount_decider("$12.5M", 0.9)
    result = extract_claim_amount(MONEY_CLAIM, decider, {}, ledger, RUN, "enforce")
    assert decider.n_calls == 1
    assert result == {
        "amount_display": "$12.5M",
        "amount_usd": 12500000.0,
        "currency": "USD",
        "kind": "raise",
    }
    state, questions = decider.calls[0]
    assert state["claim"] == MONEY_CLAIM
    assert state["candidates"] == ["$12.5M"]
    assert list(questions) == ["amount_pick", "amount_is_raise"]
    assert questions["amount_pick"]["type"] == "choice"
    assert questions["amount_pick"]["criteria"] == {
        "$12.5M": "$12.5M",
        "none": (
            "None of these states the amount the claim asserts was raised, "
            "spent, awarded, or contracted"
        ),
    }
    assert questions["amount_is_raise"]["type"] == "noul"
    row = ledger.rows[0]
    assert row["gate"] == "value_extraction"
    assert row["boundary"] == "value_extraction"
    assert row["outcome"] == "extracted"
    assert row["answers"] == {
        "amount_pick": {"choice": "$12.5M", "probabilities": {"$12.5M": 0.9}},
        "amount_is_raise": {"noul": 0.9},
    }
    assert row["noul"] == 0.9
    assert row["amount"] == result
    assert row["reason"] == "amount_display $12.5M"
    assert row["called"] is True
    assert row["deterministic_action"] == "no_amount"
    assert row["agree"] is False  # enrichment vs the no-amount baseline


def test_claim_amount_none_pick_routes_no_amount():
    """The 'none' escape: the claim carries amounts but none is what it
    asserts was raised/spent/awarded — honest absence, no enrichment."""
    ledger = DecideLedger()
    decider = _amount_decider("none", 0.95)
    assert extract_claim_amount(MONEY_CLAIM, decider, {}, ledger, RUN, "enforce") is None
    assert decider.n_calls == 1  # the pick needed Jev; only the pick is none
    row = ledger.rows[0]
    assert row["outcome"] == "no_amount"
    assert row["agree"] is True


def test_claim_amount_not_a_raise_skips():
    """A valuation span with a low raise probability: no enrichment, the row
    says why (a valuation must never become a raise figure)."""
    ledger = DecideLedger()
    decider = _amount_decider("$800M", 0.2)
    result = extract_claim_amount(VALUATION_CLAIM, decider, {}, ledger, RUN, "enforce")
    assert result is None
    row = ledger.rows[0]
    assert row["outcome"] == "skipped"
    assert row["reason"] == "not_a_raise"
    assert row["noul"] == 0.2


def test_claim_amount_verbatim_copy_is_mutate_safe():
    """The value is copied from the SPANS list, never from the model's text:
    a case-mutated pick lands on the span's own casing; a transposed pick
    matches nothing and enriches nothing."""
    ledger = DecideLedger()
    decider = _amount_decider("$12.5m", 0.9)  # case-mutated pick
    result = extract_claim_amount(MONEY_CLAIM, decider, {}, ledger, RUN, "enforce")
    assert result is not None
    assert result["amount_display"] == "$12.5M"  # the span, not the answer text
    assert result["amount_usd"] == 12500000.0

    ledger = DecideLedger()
    decider = _amount_decider("$21.5M", 0.9)  # a transposed digit: no such span
    assert extract_claim_amount(MONEY_CLAIM, decider, {}, ledger, RUN, "enforce") is None
    row = ledger.rows[0]
    assert row["outcome"] == "skipped"
    assert row["reason"] == "unknown_pick"


def test_claim_amount_normalization_table():
    """Suffix/word multipliers, comma stripping and currency classification,
    all from the span's own text. NO FX in v1: EUR/GBP amounts keep their
    face value with the currency naming it."""
    cases = [
        ("Acme raised $12.5M in Series B.", "$12.5M", 12500000.0, "USD"),
        ("Acme raised €3 million in grants.", "€3 million", 3000000.0, "EUR"),
        ("Contract USD 1,250,000 awarded.", "USD 1,250,000", 1250000.0, "USD"),
        ("Spend £1.5m on the rollout.", "£1.5m", 1500000.0, "GBP"),
        ("Raised 1.5 million dollars in angels.", "1.5 million dollars", 1500000.0, "USD"),
        ("Grant of $3k received.", "$3k", 3000.0, "USD"),
        ("Facility $2.4M closed.", "$2.4M", 2400000.0, "USD"),
    ]
    for claim, display, usd, currency in cases:
        ledger = DecideLedger()
        decider = _amount_decider(display, 0.9)
        result = extract_claim_amount(claim, decider, {}, ledger, RUN, "enforce")
        assert result == {
            "amount_display": display,
            "amount_usd": usd,
            "currency": currency,
            "kind": "raise",
        }, claim


def test_claim_amount_no_spans_no_request_no_row():
    """A claim without money spans: NO Jev call, NO row, nothing logged —
    the deterministic no-op IS the answer."""
    ledger = DecideLedger()
    decider = _amount_decider()  # would answer if asked
    assert extract_claim_amount(GOOD_CLAIM, decider, {}, ledger, RUN, "enforce") is None
    assert decider.n_calls == 0
    assert ledger.rows == []


def test_claim_amount_raise_boundary_at_min():
    """Exactly RAISE_PROB_MIN counts (>=); a hair below skips."""
    for prob, expected in ((0.5, 12500000.0), (0.49, None)):
        ledger = DecideLedger()
        decider = _amount_decider("$12.5M", prob)
        result = extract_claim_amount(MONEY_CLAIM, decider, {}, ledger, RUN, "enforce")
        assert (result["amount_usd"] if result else None) == expected, prob


def test_claim_amount_missing_pick_or_raise_skips():
    """A missing pick answer or a missing raise noul: recorded skip, no
    enrichment (the fail-closed no_answer posture)."""
    ledger = DecideLedger()
    decider = _amount_decider(pick=None, raise_prob=0.9)
    assert extract_claim_amount(MONEY_CLAIM, decider, {}, ledger, RUN, "enforce") is None
    assert ledger.rows[0]["outcome"] == "skipped"
    assert ledger.rows[0]["reason"] == "no_answer"

    ledger = DecideLedger()
    decider = _amount_decider("$12.5M", raise_prob=None)
    assert extract_claim_amount(MONEY_CLAIM, decider, {}, ledger, RUN, "enforce") is None
    assert ledger.rows[0]["outcome"] == "skipped"
    assert ledger.rows[0]["reason"] == "no_answer"


def test_claim_amount_null_decider_not_applicable_row():
    """NullDecider: no enrichment, one not-applicable row inflating no
    counts."""
    ledger = DecideLedger()
    result = extract_claim_amount(MONEY_CLAIM, NullDecider(), {}, ledger, RUN, "enforce")
    assert result is None
    assert len(ledger.rows) == 1
    assert ledger.rows[0]["called"] is False
    assert "outcome" not in ledger.rows[0]
    agg = ledger.aggregate("value_extraction")
    assert agg["counts"]["called"] == 0
    assert agg["input_tokens"] == 0


def test_claim_amount_jev_error_skips():
    """on_error skip: a Jev error enriches nothing; the error is recorded."""
    ledger = DecideLedger()
    decider = _amount_decider(ok=False)
    assert extract_claim_amount(MONEY_CLAIM, decider, {}, ledger, RUN, "enforce") is None
    row = ledger.rows[0]
    assert row["outcome"] == "errored"
    assert row["error"] == "jev_error"
    assert ledger.aggregate("value_extraction")["counts"]["errored"] == 1


def test_claim_amount_shadow_records_rows_returns_none():
    """Shadow never binds: the battery still runs (the row records what
    enforce WOULD do, amount included) while the return stays None."""
    ledger = DecideLedger()
    decider = _amount_decider("$12.5M", 0.9)
    assert extract_claim_amount(MONEY_CLAIM, decider, {}, ledger, RUN, "shadow") is None
    assert decider.n_calls == 1
    row = ledger.rows[0]
    assert row["outcome"] == "extracted"  # what enforce WOULD do
    assert row["amount"] == {
        "amount_display": "$12.5M",
        "amount_usd": 12500000.0,
        "currency": "USD",
        "kind": "raise",
    }

