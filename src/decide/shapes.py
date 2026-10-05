"""Shared dataclass shapes for the decide layer.

Plain stdlib dataclasses on purpose (house style — no pydantic): these are
simple value types crossing the decider seam and the gates; Tasks 4-6
(gates, planner, implementers) consume them, so they live here — defined
before any consumer keeps every task independently green.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Decision:
    """Outcome of one decider invocation across a batch of questions.

    Semantics (the gates table's on_error rules key off these):
    - ``applies=False`` -> gate not applicable / skipped (NullDecider, layer
      off, or the question id was unmatched). Callers take the deterministic
      path.
    - ``applies=True, ok=False`` -> the gate applies but the decision FAILED
      (Jev error / timeout / breaker). Callers apply their per-gate on_error
      rule (``drop_llm`` | ``use_deterministic``).
    - ``applies=True, ok=True`` -> verdicts are in ``answers`` keyed by
      question id; ``raw_tokens`` mirrors Jev's billed input tokens.
    """

    applies: bool
    ok: bool = True
    answers: dict = field(default_factory=dict)
    raw_tokens: int = 0


@dataclass
class PlanStep:
    """One ordered step of a planner Plan (narrow-only: no invented sources)."""

    source_ids: list[str]
    budget_knobs: dict[str, dict[str, int]]
    acceptance_criteria: list[str]


@dataclass
class Plan:
    """A validated planner output: ordered steps over the allowed sources."""

    steps: list[PlanStep]


@dataclass
class Claim:
    """An atomized claim resting on a stored document.

    ``doc_id`` is the citation anchor and REQUIRED — the claim must name the
    document it rests on (RawStore is doc_id-keyed). ``evidence_id`` is the
    dossier's ``ev-NNNN`` id: None at claim-creation time, assigned later
    during dossier packaging when ``add_evidence`` runs. ``quote_span`` is
    the verbatim quote the implementer copied from the document; None =
    not provided (the span check is skipped).
    """

    text: str
    doc_id: str
    evidence_id: str | None = None
    quote_span: str | None = None
