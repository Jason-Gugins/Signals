"""LLM planner sub-stage (MiMo): prompt build, call, validation, plan cache.

Public surface re-exported from src.llm.planner. The shared Plan/PlanStep
shapes live in src.decide.shapes (Task 2); the planner only consumes them.
Off by default — without a key + data-exit consent everything degrades to
(None, "missing_credentials") and the deterministic pipeline runs.
"""

from __future__ import annotations

from src.llm.planner import (
    API_KEY_ENV,
    CONSENT_ENV,
    DEFAULT_BASE_URL,
    DEFAULT_CACHE_DIR,
    DEFAULT_MAX_STEPS,
    DEFAULT_MODEL,
    build_prompt,
    cache_plan,
    call_planner,
    generate_plan,
    load_cached_plan,
    validate_plan,
)

__all__ = [
    "API_KEY_ENV",
    "CONSENT_ENV",
    "DEFAULT_BASE_URL",
    "DEFAULT_CACHE_DIR",
    "DEFAULT_MAX_STEPS",
    "DEFAULT_MODEL",
    "build_prompt",
    "cache_plan",
    "call_planner",
    "generate_plan",
    "load_cached_plan",
    "validate_plan",
]
