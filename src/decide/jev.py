"""Jev decider seam: protocol + Null/Mock/Live deciders + memoized factory.

The seam in one sentence: ``decide(state, questions) -> Decision`` NEVER
raises past it — every failure mode (HTTP 4xx/5xx, timeout, connection
error, malformed payload, tripped breaker) comes back as
``Decision(applies=True, ok=False)`` so the calling gate can apply its
configured ``on_error`` rule.

- ``NullDecider``  — no key / mode off / out of scope: every gate returns
  "not applicable" (``applies=False``); the deterministic pipeline runs.
- ``MockDecider``  — scripted verdicts for tests (exact id, then longest
  question-id prefix, else not applicable).
- ``LiveDecider``  — thin client over a bare ``httpx.Client`` (NOT
  ``src/core/http.py``'s HttpFetcher, which is a robots/ratelimit fetch
  machine shaped around FetchTask). One jittered retry on 5xx/transport
  failure; error-rate breaker with a minimum sample.

The factory ``get_decider()`` is memoized with double-checked locking, the
same pattern as ``rerank.get_scorer()`` (src/signals/rerank.py). The
data-exit consent env var gates ANY live outbound call: missing keys or
consent means deterministic behavior — never an error.

Keys are never logged: no log line carries the Authorization header or the
api_key value.
"""

from __future__ import annotations

import os
import random
import threading
import time
from typing import Protocol, runtime_checkable

import httpx
from loguru import logger

from src.core.config import Config
from src.decide.shapes import Decision

# The data-exit consent string: ANY live outbound LLM call (Jev, planner,
# implementers — shadow as well as enforce) requires it verbatim in the env.
# It lives in the untracked .env, not in committed config.
DATA_EXIT_CONSENT = "I-understand-document-text-leaves-this-machine"

# Breaker minimum sample: one transient error must not disable Jev for a
# whole run, so the error rate is only evaluated once this many calls have
# actually gone out.
BREAKER_MIN_CALLS = 5

DEFAULT_MODEL = "jev-1.13.0"
DEFAULT_BASE_URL = "https://api.typesafe.ai/v1/systemone"

# Injectable retry-backoff sleep (tests monkeypatch this to a no-op).
_backoff_sleep = time.sleep


@runtime_checkable
class Decider(Protocol):
    """The seam the gates call into.

    ``state`` is an arbitrary JSON-serializable payload (str or dict);
    ``questions`` maps question-id -> spec dict
    (``{"type": "noul"|"choice"|"score", "instructions": str,
    "criteria": [...]}`` — for noul, criteria may be omitted).
    """

    def decide(self, state: str | dict, questions: dict[str, dict]) -> Decision: ...


class NullDecider:
    """Skips every gate: not applicable, deterministic path, zero tokens."""

    def decide(self, state: str | dict, questions: dict[str, dict]) -> Decision:
        return Decision(applies=False, ok=True, answers={}, raw_tokens=0)


class MockDecider:
    """Scripted verdicts for offline tests.

    Exact question-id match first, then the LONGEST question-id-prefix match
    (so a verdict keyed ``"claim"`` answers question ``"claim_0"``), else
    ``Decision(applies=False)``.
    """

    def __init__(self, verdicts: dict[str, Decision]) -> None:
        self.verdicts = dict(verdicts)

    def decide(self, state: str | dict, questions: dict[str, dict]) -> Decision:
        for qid in questions:
            if qid in self.verdicts:
                return self.verdicts[qid]
        best: tuple[int, Decision] | None = None
        for qid in questions:
            for key, decision in self.verdicts.items():
                if qid.startswith(key) and (best is None or len(key) > best[0]):
                    best = (len(key), decision)
        if best is not None:
            return best[1]
        return Decision(applies=False, ok=True, answers={}, raw_tokens=0)


class LiveDecider:
    """Thin Jev client. Failure modes collapse to ``ok=False`` Decisions."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        timeout_s: float = 5.0,
        max_retries: int = 1,
        error_rate_breaker: float = 0.20,
    ) -> None:
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.error_rate_breaker = error_rate_breaker
        # Error-rate breaker bookkeeping (per instance == per run). A call
        # counts once per decide() invocation that passes the breaker check;
        # it is an error only if the invocation ultimately fails.
        self.total_calls = 0
        self.errors = 0
        self._client = httpx.Client(timeout=timeout_s)

    def close(self) -> None:
        """Release the connection pool (hygiene; not required for correctness)."""
        self._client.close()

    def decide(self, state: str | dict, questions: dict[str, dict]) -> Decision:
        # Breaker check BEFORE the call: sustained failure stops the network
        # chatter entirely; the minimum sample keeps one transient error from
        # disabling Jev for the rest of the run.
        if self._breaker_tripped():
            logger.debug(
                "jev breaker tripped (errors={}/{}, threshold={}); failing closed",
                self.errors,
                self.total_calls,
                self.error_rate_breaker,
            )
            return Decision(applies=True, ok=False, answers={}, raw_tokens=0)

        self.total_calls += 1
        payload = {"state": state, "model": self.model, "questions": questions}
        # Never logged: the Authorization header / api_key stay out of log args.
        headers = {"Authorization": f"Bearer {self.api_key}"}

        for attempt in range(self.max_retries + 1):
            if attempt > 0:
                _backoff_sleep(random.uniform(0.05, 0.5))
            try:
                resp = self._client.post(
                    self.base_url, json=payload, headers=headers, timeout=self.timeout_s
                )
                if resp.status_code >= 500:
                    logger.debug("jev 5xx on attempt {}: {}", attempt + 1, resp.status_code)
                    continue  # retryable
                if resp.status_code >= 400:
                    logger.debug("jev 4xx: {}; no retry", resp.status_code)
                    return self._failed()
                try:
                    body = resp.json()
                except ValueError:
                    logger.debug("jev malformed JSON body; no retry")
                    return self._failed()
                return self._parse(body)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                logger.debug(
                    "jev transport failure on attempt {}: {}",
                    attempt + 1,
                    type(exc).__name__,
                )
                continue  # transient network-level failure -> retryable

        self.errors += 1
        return Decision(applies=True, ok=False, answers={}, raw_tokens=0)

    def _breaker_tripped(self) -> bool:
        if self.total_calls < BREAKER_MIN_CALLS:
            return False
        return self.errors / self.total_calls > self.error_rate_breaker

    def _failed(self) -> Decision:
        self.errors += 1
        return Decision(applies=True, ok=False, answers={}, raw_tokens=0)

    def _parse(self, body: object) -> Decision:
        """Normalize (copy only) a 200 response into a Decision."""
        if not isinstance(body, dict) or not isinstance(body.get("answers"), dict):
            # Valid JSON, wrong shape: the gate could do nothing with it —
            # fail the decision rather than hand back silent emptiness.
            logger.debug("jev response missing answers map; failing the decision")
            return self._failed()
        # Keep the answers map as-is (normalized only by copying): gates read
        # the typed fields (choice/score/noul, probabilities, confidence).
        answers = dict(body["answers"])
        usage = body.get("usage") or {}
        # Jev bills INPUT tokens only — usage.output_tokens exists but is not
        # billed, so raw_tokens mirrors input_tokens.
        raw_tokens = int(usage.get("input_tokens", 0) or 0)
        return Decision(applies=True, ok=True, answers=answers, raw_tokens=raw_tokens)


# Memoized factory — double-checked locking, the rerank.get_scorer() pattern
# (the collector thread and the watch loop can both reach get_decider()).
_decider_lock = threading.Lock()
_decider_instance: Decider | None = None
_decider_key: tuple[str, str, str] | None = None


def _reset_decider_cache() -> None:
    """Test helper: drop the memoized instance so the next get_decider()
    re-evaluates config + env."""
    global _decider_instance, _decider_key
    with _decider_lock:
        _decider_instance = None
        _decider_key = None


def get_decider(cfg: dict | None = None) -> Decider:
    """Return the process-wide decider for the given decide-layer config.

    cfg=None loads config/decide.yaml via the Config.load_yaml seam; a
    missing file (FileNotFoundError) means the layer is fully off. The caller
    (Task 3's policy) applies the SIGNALS_DECIDE_MODE env override BEFORE
    calling; here any mode == "off" is off.

    A LiveDecider is attached ONLY when mode != "off" AND TYPESAFE_API_KEY is
    set AND SIGNALS_DECIDE_DATA_EXIT equals DATA_EXIT_CONSENT — otherwise a
    NullDecider. Missing keys or consent is deterministic behavior, never an
    error.
    """
    global _decider_instance, _decider_key

    if cfg is None:
        try:
            cfg = Config().load_yaml("decide") or {}
        except FileNotFoundError:
            cfg = {}

    mode = cfg.get("mode", "off")
    # YAML 1.1 parses a bare `off` as boolean False — treat falsy modes as
    # off too; the schema quotes it, this is just belt-and-braces.
    mode_off = mode is None or mode is False or mode == "off"
    decider_cfg = cfg.get("decider") or {}
    model = str(decider_cfg.get("model", DEFAULT_MODEL))
    base_url = str(decider_cfg.get("base_url", DEFAULT_BASE_URL))
    key = (str(mode), model, base_url)

    if _decider_instance is not None and _decider_key == key:
        return _decider_instance
    with _decider_lock:
        if _decider_instance is not None and _decider_key == key:
            return _decider_instance
        if mode_off:
            instance: Decider = NullDecider()
        else:
            api_key = os.environ.get("TYPESAFE_API_KEY")
            consent = os.environ.get("SIGNALS_DECIDE_DATA_EXIT")
            if api_key and consent == DATA_EXIT_CONSENT:
                instance = LiveDecider(
                    base_url=base_url,
                    model=model,
                    api_key=api_key,
                    timeout_s=float(decider_cfg.get("timeout_s", 5.0)),
                    max_retries=int(decider_cfg.get("max_retries", 1)),
                    error_rate_breaker=float(decider_cfg.get("error_rate_breaker", 0.20)),
                )
            else:
                logger.debug(
                    "decide layer on but no live decider: missing TYPESAFE_API_KEY "
                    "or SIGNALS_DECIDE_DATA_EXIT consent -> NullDecider"
                )
                instance = NullDecider()
        _decider_instance = instance
        _decider_key = key
        return instance
