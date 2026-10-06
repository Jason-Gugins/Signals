"""Tests for decide-layer policy (src/decide/policy.py).

Mode resolution (env override + config, fail-safe off), the gate-action
composition ladder, and code-level scope enforcement: only stack frames
whose (name, file-basename) pair is in ``_ALLOWED_FRAMES`` — ``run_intel``
in intel.py, ``discover``/``discover_competitors`` in orchestrator.py —
may attach a live decider; every other call site (collect/score, watch/
scheduler, CLI, tests) gets a NullDecider.

Fully offline: no network anywhere. The scope tests construct a
LiveDecider only to exercise attach_decider's frame inspection — attaching
makes no calls, and the refused path returns NullDecider (decide() never
leaves the process).
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

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

REPO_ROOT = Path(__file__).resolve().parents[1]

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
    # Attaching null anywhere is always safe — no allowed frame needed.
    assert attach_decider(null) is null


def test_attach_decider_refuses_outside_allowed_frames():
    live = _live()
    try:
        # This test function is not one of the allowed frame names -> the
        # live decider is refused (no monkeypatching: the production
        # _ALLOWED_FRAMES set decides).
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
    # Point the allowed-frames set at THIS file so the run_intel() above
    # counts as an in-scope call site.
    monkeypatch.setattr(policy, "_ALLOWED_FRAMES", {("run_intel", os.path.basename(__file__))})
    live = _live()
    try:
        # Identity: the live decider passes through untouched.
        assert run_intel(live) is live
    finally:
        live.close()


def discover(handle):
    """Scope marker under test: named discover in the (patched) orchestrator file."""
    return attach_decider(handle)


def test_attach_decider_allows_discover_frame(monkeypatch):
    # The human-triggered discover() sweep entry point may attach a live
    # decider (wave-2/3 Task 7: entity alignment). Patch the allowed set so
    # the (name, file-basename) pair matches THIS module's discover() above.
    monkeypatch.setattr(policy, "_ALLOWED_FRAMES", {("discover", os.path.basename(__file__))})
    live = _live()
    try:
        assert discover(live) is live
    finally:
        live.close()


def discover_competitors(handle):
    """Scope marker under test: named discover_competitors in the (patched) orchestrator file."""
    return attach_decider(handle)


def test_attach_decider_allows_discover_competitors_frame(monkeypatch):
    # Same contract as discover(): the discover_competitors() sweep entry
    # point may attach. Patch the allowed set so the pair matches.
    monkeypatch.setattr(
        policy, "_ALLOWED_FRAMES", {("discover_competitors", os.path.basename(__file__))}
    )
    live = _live()
    try:
        assert discover_competitors(live) is live
    finally:
        live.close()


def test_attach_decider_still_refuses_run_intel_wrong_file(monkeypatch):
    # The production set names run_intel@intel.py: a run_intel frame in any
    # OTHER file (this test module) fails the (name, file-basename) pair
    # check and gets NullDecider.
    monkeypatch.setattr(
        policy,
        "_ALLOWED_FRAMES",
        {
            ("run_intel", "intel.py"),
            ("discover", "orchestrator.py"),
            ("discover_competitors", "orchestrator.py"),
        },
    )
    live = _live()
    try:
        result = run_intel(live)
        assert isinstance(result, NullDecider)
        assert result is not live
    finally:
        live.close()


def collect(handle):
    """Scope marker under test: watch/scheduler cycles call collect() — never allowed."""
    return attach_decider(handle)


def test_attach_decider_still_refuses_collect_frame(monkeypatch):
    # An allowed FILE does not authorize other names in it: orchestrator.py
    # hosts the allowed discover/discover_competitors entry points, but the
    # watch/scheduler cycles call collect() directly — a collect frame must
    # never attach. Simulated here by allowing the TEST file's basename for
    # discover only: the pair check still refuses collect because its NAME
    # is not an allowed one.
    monkeypatch.setattr(policy, "_ALLOWED_FRAMES", {("discover", os.path.basename(__file__))})
    live = _live()
    try:
        result = collect(live)
        assert isinstance(result, NullDecider)
        assert result is not live
    finally:
        live.close()


def test_attach_decider_refuses_mock_in_production():
    # MockDecider is a test double — it never attaches in production code,
    # even though its presence is harmless (no network). Refuse it anyway.
    mock = MockDecider({})
    result = attach_decider(mock)
    assert isinstance(result, NullDecider)
    assert mock.verdicts == {}


# --- wave-2/3 Task 1: decide.yaml config gates -------------------------------


def test_decide_yaml_wave23_keys():
    """decide.yaml gains the five wave-2/3 gate keys; existing entries are unchanged."""
    with open(REPO_ROOT / "config" / "decide.yaml", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    gates = cfg["decider"]["gates"]
    # Completeness-verify cascade: max-style escalation battery over the
    # five dossier fields.
    assert gates["completeness_verify"] == {
        "enabled": True,
        "fire_threshold": 0.70,
        "max_escalations": 20,
        "on_error": "skip",
    }
    # Taxonomy typing: confident above 0.90, ambiguous below 0.60.
    assert gates["taxonomy_typing"] == {
        "enabled": True,
        "confident": 0.90,
        "ambiguous": 0.60,
        "on_error": "skip",
    }
    # Event dates: review when the date confidence drops below 0.60.
    assert gates["event_dates"] == {
        "enabled": True,
        "review_below": 0.60,
        "on_error": "skip",
    }
    # Value extraction: pre-parsed values carried via evidence_data.
    assert gates["value_extraction"] == {"enabled": True, "on_error": "skip"}
    # Evidence re-rank: keep_n 0 = no cap; rides the document-gate request.
    assert gates["evidence_rerank"] == {
        "enabled": True,
        "keep_n": 0,
        "on_error": "keep_order",
    }
    # Wave-1 entries stay untouched.
    assert gates["plan_qualification"] == {"enabled": True, "floor": 0.70, "on_error": "drop_llm"}
    assert gates["posture_audit"] == {"enabled": True, "shadow_only": True}
    assert gates["routing"] == {
        "enabled": True,
        "route_threshold": 2000,
        "on_error": "use_deterministic",
    }
    assert gates["citation_soundness"] == {
        "enabled": True,
        "floor": 0.70,
        "band_low": 0.30,
        "on_error": "drop_llm",
    }
    assert gates["need_promotion"] == {
        "enabled": True,
        "floor": 0.70,
        "band_low": 0.30,
        "on_error": "drop_llm",
    }
    assert gates["document_gate"] == {
        "enabled": True,
        "relevant_min": 0.45,
        "evidence_min": 0.55,
        "injection_max": 0.70,
        "on_error": "use_deterministic",
    }
    assert gates["output_screen"] == {
        "enabled": True,
        "review_threshold": 0.35,
        "action_threshold": 0.70,
        "on_error": "use_deterministic",
    }
