"""Decide layer: Jev decider seam, shared shapes, policy; gates come later.

Public surface (Task 2): the shared Plan/Claim shapes and the Decider
protocol with Null/Mock/Live implementations plus the memoized get_decider
factory. Task 3 adds the policy building blocks: mode resolution
(resolve_mode), the gate-action composition ladder (compose), and
code-level scope enforcement (attach_decider — only run_intel attaches a
live decider). Off by default — without keys/consent everything degrades
to NullDecider and the deterministic pipeline.
"""

from __future__ import annotations

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
    "Decision",
    "Decider",
    "LiveDecider",
    "MockDecider",
    "NullDecider",
    "Plan",
    "PlanStep",
    "attach_decider",
    "compose",
    "get_decider",
    "resolve_mode",
]
