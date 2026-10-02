"""Tests for decide-layer policy (src/decide/policy.py).

Mode resolution (env override + config, fail-safe off), the gate-action
composition ladder, and code-level scope enforcement: only a frame named
``run_intel`` in intel.py may attach a live decider — every other call site
gets a NullDecider.

Fully offline: no network anywhere. The scope tests construct a
LiveDecider only to exercise attach_decider's frame inspection — attaching
makes no calls, and the refused path returns NullDecider (decide() never
leaves the process).
"""

from __future__ import annotations

import os

import src.decide.policy as policy
from src.decide import (
    LiveDecider,
    MockDecider,
    NullDecider,
    attach_decider,
    compose,
    resolve_mode,
)

BASE = "https://api.typesafe.ai/v1/systemone"
MODE_ENV = "SIGNALS_DECIDE_MODE"

# Weak -> strong: compose() must return the stronger of its two actions.
ACTIONS = ("allow", "ask", "hold", "deny")


def _live() -> LiveDecider:
    return LiveDecider(base_url=BASE, model="jev-1.13.0", api_key="test-dummy")


# --- resolve_mode -----------------------------------------------------------


def test_resolve_mode_defaults_off(monkeypatch):
    monkeypatch.delenv(MODE_ENV, raising=False)
    assert resolve_mode() == "off"  # no cfg, no env
    assert resolve_mode(None) == "off"
    assert resolve_mode({}) == "off"
    assert resolve_mode({"mode": "off"}) == "off"
    assert resolve_mode({"mode": "shadow"}) == "shadow"
    assert resolve_mode({"mode": "enforce"}) == "enforce"


def test_resolve_mode_env_overrides_config(monkeypatch):
    monkeypatch.setenv(MODE_ENV, "shadow")
    assert resolve_mode({"mode": "enforce"}) == "shadow"
    monkeypatch.setenv(MODE_ENV, "enforce")
    assert resolve_mode({"mode": "off"}) == "enforce"
    # Env cleared -> the config value wins again.
    monkeypatch.delenv(MODE_ENV)
    assert resolve_mode({"mode": "enforce"}) == "enforce"
    assert resolve_mode({"mode": "off"}) == "off"


def test_resolve_mode_unknown_falls_back_off(monkeypatch):
    monkeypatch.delenv(MODE_ENV, raising=False)
    # Invalid config values (typo, stray YAML 1.1 bare `off` -> boolean
    # False) warn and fail safe to "off".
    assert resolve_mode({"mode": "bogus"}) == "off"
    assert resolve_mode({"mode": False}) == "off"
    # Invalid env override: the override simply does not apply (warning),
    # config is consulted — with no config mode that is still "off".
    monkeypatch.setenv(MODE_ENV, "bogus")
    assert resolve_mode() == "off"
    assert resolve_mode({}) == "off"


# --- compose ----------------------------------------------------------------


def test_compose_ladder():
    for a in ACTIONS:
        for b in ACTIONS:
            expected = max(a, b, key=ACTIONS.index)  # later in ladder = stronger
            assert compose(a, b) == expected, (a, b)
    # deny survives every composition (nothing upgrades a deny).
    for a in ACTIONS:
        assert compose(a, "deny") == "deny"
        assert compose("deny", a) == "deny"
    assert compose("allow", "allow") == "allow"


def test_compose_unknown_input_tightens_to_hold():
    # Unknown action strings tighten to "hold" (reversible, never opens a
    # gate) with a warning — documented choice, pinned here. "deny" would be
    # too aggressive for a composition helper, "allow" too permissive.
    assert compose("bogus", "allow") == "hold"
    assert compose("allow", "bogus") == "hold"
    assert compose("bogus", "deny") == "deny"  # unknown -> hold, deny still wins
    assert compose(None, "allow") == "hold"  # type: ignore[arg-type]
    assert compose("ALLOW", "allow") == "hold"  # ladder is lowercase-exact


# --- attach_decider (code-level scope) --------------------------------------


def test_attach_decider_passes_null_through():
    null = NullDecider()
    # Attaching null anywhere is always safe — no run_intel frame needed.
    assert attach_decider(null) is null


def test_attach_decider_refuses_outside_run_intel():
    live = _live()
    try:
        # This test function is NOT run_intel -> the live decider is refused.
        result = attach_decider(live)
        assert isinstance(result, NullDecider)
        # attach made no network call, and the refused handle behaves null.
        d = result.decide("state", {"q": {"type": "noul", "instructions": "x"}})
        assert d.applies is False
        assert d.ok is True
        assert d.raw_tokens == 0
    finally:
        live.close()


def run_intel(handle):
    """Scope marker under test: named run_intel in the (patched) intel file."""
    return attach_decider(handle)


def test_attach_decider_allows_run_intel_frame(monkeypatch):
    # Point the scope constant at THIS file so the run_intel() above counts
    # as the one in-scope call site.
    monkeypatch.setattr(policy, "_INTEL_FILENAME", os.path.basename(__file__))
    live = _live()
    try:
        # Identity: the live decider passes through untouched.
        assert run_intel(live) is live
    finally:
        live.close()


def test_attach_decider_refuses_mock_in_production():
    # MockDecider is a test double — it never attaches in production code,
    # even though its presence is harmless (no network). Refuse it anyway.
    mock = MockDecider({})
    result = attach_decider(mock)
    assert isinstance(result, NullDecider)
    assert mock.verdicts == {}
