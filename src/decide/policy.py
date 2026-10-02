"""Decide-layer policy: mode resolution, action composition, scope.

Three small offline building blocks the gates and the run_intel wiring
(Task 7) sit on:

- ``resolve_mode(cfg)`` — which mode is the layer in? The
  ``SIGNALS_DECIDE_MODE`` env override wins when it holds a valid mode,
  otherwise ``config/decide.yaml``'s ``mode`` key decides; anything
  unrecognized fails safe to ``off`` (NullDecider, deterministic pipeline).
- ``compose(a, b)`` — the gate composition ladder over
  ``{"allow", "ask", "hold", "deny"}``: deny > hold > ask > allow. Later
  checks tighten, never skip; nothing upgrades a ``deny``.
- ``attach_decider(handle)`` — code-level scope enforcement. The ONLY call
  site allowed to attach a live decider is ``run_intel`` in
  ``src/pipeline/intel.py`` (wired in Task 7); every other entry point
  (orchestrator collect/score/watch_loop/sweep/run_all, CLI) gets a
  ``NullDecider``. Watch/scheduler cycles cannot reach the decide layer by
  construction — they call ``orch.collect``/``orch.score`` directly and
  never ``run_intel`` — this guard is defense-in-depth on top of that.
"""

from __future__ import annotations

import inspect
import os

from loguru import logger

from src.decide.jev import Decider, LiveDecider, NullDecider

# Valid decide-layer modes (config/decide.yaml `mode`, quoted; the env
# override uses the same set). Anything else fails safe to "off".
_VALID_MODES = frozenset({"off", "shadow", "enforce"})

# Env override: set to a valid mode it beats the config file (operator
# per-run lever); an invalid value is ignored with a warning.
_MODE_ENV_VAR = "SIGNALS_DECIDE_MODE"

# Basename of the only module whose run_intel() may attach a live decider.
# A module constant so tests can point it at a test file; production code
# never reassigns it.
_INTEL_FILENAME = "intel.py"

# Gate actions, weak -> strong. compose() returns the stronger of its two
# arguments: a later check can only tighten the outcome, never skip or
# upgrade an earlier deny.
_ACTION_STRENGTH: dict[str, int] = {"allow": 0, "ask": 1, "hold": 2, "deny": 3}

# What an unknown action string composes as (see compose()).
_UNKNOWN_ACTION = "hold"


def _normalize_mode(raw: object) -> str | None:
    """Return the canonical mode string, or None when unrecognized.

    Tolerates case/whitespace (env vars are typed by humans); a stray YAML
    1.1 boolean (bare `off` parses as False) normalizes to None -> off.
    """
    if raw is None:
        return None
    value = str(raw).strip().lower()
    return value if value in _VALID_MODES else None


def resolve_mode(cfg: dict | None = None) -> str:
    """Resolve the decide-layer mode: env override first, then config, off last.

    Precedence:
    1. ``SIGNALS_DECIDE_MODE`` when set to a valid mode (case/whitespace
       tolerated) — it beats the config file.
    2. Otherwise ``cfg["mode"]`` (``cfg=None`` or ``cfg={}`` means "off").
    3. Any unrecognized value warns and returns ``"off"`` — fail-safe. An
       invalid env value is ignored as an override (warning) and the config
       value is consulted; an invalid config value is always ``"off"``.
       Documented judgment call: the override simply does not apply when it
       is not a valid mode, matching the committed-and-reviewed config as
       the fallback; the warning keeps the typo visible.
    """
    env_raw = os.environ.get(_MODE_ENV_VAR)
    if env_raw is not None:
        env_mode = _normalize_mode(env_raw)
        if env_mode is not None:
            return env_mode
        logger.warning(
            "decide layer: invalid {}={!r}; env override ignored, config decides",
            _MODE_ENV_VAR,
            env_raw,
        )
    cfg_mode = (cfg or {}).get("mode", "off")
    mode = _normalize_mode(cfg_mode)
    if mode is None:
        logger.warning(
            "decide layer: invalid mode {!r} in config; failing safe to 'off'", cfg_mode
        )
        return "off"
    return mode


def compose(a: str, b: str) -> str:
    """Return the stronger of two gate actions (deny > hold > ask > allow).

    Pure composition ladder: later checks tighten, never skip; nothing
    upgrades a ``deny``. Unknown action strings (typo, hostile value, None)
    compose as ``"hold"`` — documented choice: ``"deny"`` would be too
    aggressive for a composition helper (one bad string could brick a whole
    run), ``"allow"`` would silently open the gate; ``"hold"`` tightens but
    stays reversible, and a warning is logged so the bad value is visible.
    The ladder is lowercase-exact: an out-of-ladder value never passes
    through unchanged, so callers can rely on the return being one of the
    four real actions.
    """
    strength_a = _ACTION_STRENGTH.get(a)
    if strength_a is None:
        logger.warning("decide layer: unknown gate action {!r}; composing as 'hold'", a)
        a, strength_a = _UNKNOWN_ACTION, _ACTION_STRENGTH[_UNKNOWN_ACTION]
    strength_b = _ACTION_STRENGTH.get(b)
    if strength_b is None:
        logger.warning("decide layer: unknown gate action {!r}; composing as 'hold'", b)
        b, strength_b = _UNKNOWN_ACTION, _ACTION_STRENGTH[_UNKNOWN_ACTION]
    return a if strength_a >= strength_b else b


def _in_run_intel() -> bool:
    """True when the call stack contains ``run_intel`` from intel.py.

    Frame walk via ``inspect.stack()``: a frame qualifies only when its
    code object is NAMED ``run_intel`` AND its file basename equals
    ``_INTEL_FILENAME`` — a function of the same name elsewhere (tests,
    another module) does not count unless the constant is repointed.
    """
    for frame_info in inspect.stack():
        code = frame_info.frame.f_code
        if code.co_name == "run_intel" and os.path.basename(code.co_filename) == _INTEL_FILENAME:
            return True
    return False


def attach_decider(handle: Decider) -> Decider:
    """Attach a decider to the decide layer, enforcing the run_intel scope.

    - ``NullDecider`` passes through unchanged: attaching null anywhere is
      always safe (it is what every out-of-scope entry point ends up with).
    - A ``LiveDecider`` passes through unchanged ONLY when called from
      ``run_intel`` in intel.py — the sole production call site (Task 7).
    - Anything else (a LiveDecider out of scope, or any other decider such
      as a MockDecider reaching production code) warns and returns a fresh
      ``NullDecider()``. Never raises: a scope violation degrades to the
      deterministic pipeline, it does not crash the run.
    """
    if isinstance(handle, NullDecider):
        return handle
    if isinstance(handle, LiveDecider) and _in_run_intel():
        return handle
    logger.warning(
        "decide layer: live decider attach refused outside run_intel (handle={})",
        type(handle).__name__,
    )
    return NullDecider()
