"""Decide layer: Jev decider seam, shared shapes, policy, gates.

Public surface (Task 2): the shared Plan/Claim shapes and the Decider
protocol with Null/Mock/Live implementations plus the memoized get_decider
factory. Task 3 adds the policy building blocks: mode resolution
(resolve_mode), the gate-action composition ladder (compose), and
code-level scope enforcement (attach_decider — only run_intel attaches a
live decider). Task 4 adds the five boundary gates (G1 plan qualification,
G2 posture audit, G3 routing, G4 citation soundness, G5 need promotion) with
the per-run DecideLedger and the shared state helpers. Off by default —
without keys/consent everything degrades to NullDecider and the
deterministic pipeline.
"""

from __future__ import annotations

from src.decide.gates import (
    DOC_EVIDENCE_MIN,
    DOC_INJECTION_MAX,
    DOC_RELEVANT_MIN,
    LEXICAL_OVERLAP_FLOOR,
    OUTPUT_ACTION_THRESHOLD,
    OUTPUT_REVIEW_THRESHOLD,
    DecideLedger,
    anchor_window,
    estimate_tokens,
    gate_citation_batch,
    gate_document,
    gate_need_promotion,
    gate_plan_step,
    gate_posture_audit,
    gate_routing,
    lexical_overlap,
    state_hash,
)
from src.decide.jev import (
    Decider,
    LiveDecider,
    MockDecider,
    NullDecider,
    get_decider,
)
from src.decide.policy import attach_decider, compose, resolve_mode
from src.decide.shapes import Claim, Decision, Plan, PlanStep

__all__ = [
    "Claim",
    "DecideLedger",
    "Decision",
    "Decider",
    "DOC_EVIDENCE_MIN",
    "DOC_INJECTION_MAX",
    "DOC_RELEVANT_MIN",
    "LEXICAL_OVERLAP_FLOOR",
    "LiveDecider",
    "MockDecider",
    "NullDecider",
    "OUTPUT_ACTION_THRESHOLD",
    "OUTPUT_REVIEW_THRESHOLD",
    "Plan",
    "PlanStep",
    "anchor_window",
    "attach_decider",
    "compose",
    "estimate_tokens",
    "gate_citation_batch",
    "gate_document",
    "gate_need_promotion",
    "gate_plan_step",
    "gate_posture_audit",
    "gate_routing",
    "get_decider",
    "lexical_overlap",
    "resolve_mode",
    "state_hash",
]
