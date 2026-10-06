"""Decide layer: Jev decider seam, shared shapes, policy, gates.

Public surface (Task 2): the shared Plan/Claim shapes and the Decider
protocol with Null/Mock/Live implementations plus the memoized get_decider
factory. Task 3 adds the policy building blocks: mode resolution
(resolve_mode), the gate-action composition ladder (compose), and
code-level scope enforcement (attach_decider — only run_intel attaches a
live decider). Task 4 adds the five boundary gates (G1 plan qualification,
G2 posture audit, G3 routing, G4 citation soundness, G5 need promotion) with
the per-run DecideLedger and the shared state helpers. Task 6 adds the
consistency sampler (sample_consistency) for repeat-and-measure calibration
audits. Off by default —
without keys/consent everything degrades to NullDecider and the
deterministic pipeline.
"""

from __future__ import annotations

from src.decide.audit import sample_consistency
from src.decide.gates import (
    AMBIGUOUS,
    CONFIDENT,
    DOC_EVIDENCE_MIN,
    DOC_INJECTION_MAX,
    DOC_RELEVANT_MIN,
    FIRE_THRESHOLD,
    LEXICAL_OVERLAP_FLOOR,
    OUTPUT_ACTION_THRESHOLD,
    OUTPUT_REVIEW_THRESHOLD,
    REVIEW_BELOW,
    SEPARATION_MIN,
    DecideLedger,
    anchor_window,
    classify_claims,
    estimate_tokens,
    extract_event_date,
    gate_citation_batch,
    gate_completeness,
    gate_document,
    gate_need_promotion,
    gate_plan_step,
    gate_posture_audit,
    gate_routing,
    lexical_overlap,
    separation_ratio,
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
    "AMBIGUOUS",
    "Claim",
    "CONFIDENT",
    "DecideLedger",
    "Decision",
    "Decider",
    "DOC_EVIDENCE_MIN",
    "DOC_INJECTION_MAX",
    "DOC_RELEVANT_MIN",
    "FIRE_THRESHOLD",
    "LEXICAL_OVERLAP_FLOOR",
    "LiveDecider",
    "MockDecider",
    "NullDecider",
    "OUTPUT_ACTION_THRESHOLD",
    "OUTPUT_REVIEW_THRESHOLD",
    "Plan",
    "PlanStep",
    "REVIEW_BELOW",
    "SEPARATION_MIN",
    "anchor_window",
    "attach_decider",
    "classify_claims",
    "compose",
    "estimate_tokens",
    "extract_event_date",
    "gate_citation_batch",
    "gate_completeness",
    "gate_document",
    "gate_need_promotion",
    "gate_plan_step",
    "gate_posture_audit",
    "gate_routing",
    "get_decider",
    "lexical_overlap",
    "resolve_mode",
    "sample_consistency",
    "separation_ratio",
    "state_hash",
]
