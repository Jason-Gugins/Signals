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
  failure; error-rate breaker with a minimum sample and a per-gate
  exclusion list (chronic minor-gate failures — routing, posture_audit in
  the shipped config — never trip the breaker that fail-closes G4). The
  per-request timeout SCALES with question count
  (:func:`effective_timeout`). Failed Decisions carry ``cause``, the short
  summary the ledger's ``error`` column emits.

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
from src.decide.shapes import Decision, exception_cause

# The data-exit consent string: ANY live outbound LLM call (Jev, planner,
# implementers — shadow as well as enforce) requires it verbatim in the env.
# It lives in the untracked .env, not in committed config.
DATA_EXIT_CONSENT = "I-understand-document-text-leaves-this-machine"

# Breaker minimum sample: one transient error must not disable Jev for a
# whole run, so the error rate is only evaluated once this many calls have
# actually gone out. Overridable via the decider block's ``breaker.min_calls``.
BREAKER_MIN_CALLS = 5

# Default breaker keys (the ``breaker`` block in the decider config):
BREAKER_THRESHOLD = 0.20

# Timeout scaling: the configured ``timeout_s`` is the BASE for a 3-question
# request; every question beyond the first three adds ``per_question_s``
# seconds (the decide.yaml comment's long-promised scaling, now real).
DEFAULT_PER_QUESTION_S = 0.5
_TIMEOUT_FREE_QUESTIONS = 3


def effective_timeout(timeout_s: float, per_question_s: float, n_questions: int) -> float:
    """``timeout_s + per_question_s * max(0, n_questions - 3)`` — a pure
    formula (unit-tested directly); decide() applies it per request."""
    return float(timeout_s) + float(per_question_s) * max(0, int(n_questions) - _TIMEOUT_FREE_QUESTIONS)

DEFAULT_MODEL = "jev-1.13.0"
DEFAULT_BASE_URL = "https://api.typesafe.ai/v1/systemone"

# Injectable retry-backoff sleep (tests monkeypatch this to a no-op).
_backoff_sleep = time.sleep

# Cause snippet cap: an HTTP error cause carries this many characters of the
# response body (the 422 body named the taxonomy root cause; the old generic
# "HTTPStatusError: 422" line discarded it).
_HTTP_BODY_SNIPPET_CHARS = 110


def _wire_questions(questions: dict[str, dict] | None) -> dict[str, dict]:
    """Boundary normalization for the POST body's questions.

    TypeSafe's schema requires a choice question's ``criteria`` to be an
    OBJECT (``{label: description}`` — pydantic dict_type), but gates may
    ship a LIST of labels, so any list is copied to ``{label: ""}``. One
    point, all gates: this healed taxonomy typing (54-entry list), posture
    audit and G3 routing — every gate that had 422'd since birth — while
    dict-criteria gates (G4, event-dates) pass through byte-identically.
    Dict criteria are untouched; absent/None criteria stay absent. The
    caller's question dicts are NEVER mutated: gates re-read their
    questions' labels after the call for answer routing, and TypeSafe
    returns the dict keys as the choice labels those gates route on.
    """
    if not isinstance(questions, dict):
        return questions
    wire: dict[str, dict] = {}
    for qid, spec in questions.items():
        if isinstance(spec, dict) and isinstance(spec.get("criteria"), list):
            copied = dict(spec)
            copied["criteria"] = {str(label): "" for label in spec["criteria"]}
            wire[qid] = copied
        else:
            wire[qid] = spec
    return wire


@runtime_checkable
class Decider(Protocol):
    """The seam the gates call into.

    ``state`` is an arbitrary JSON-serializable payload (str or dict);
    ``questions`` maps question-id -> spec dict
    (``{"type": "noul"|"choice"|"score", "instructions": str,
    "criteria": ...}`` — for noul, criteria may be omitted). A choice
    question's ``criteria`` may be EITHER shape: a LIST of labels or a DICT
    (``{label: description}``). LiveDecider normalizes lists to
    ``{label: ""}`` at the request boundary — TypeSafe's schema requires an
    object (pydantic dict_type) and returns the dict keys as the choice
    labels gates.py's ``_choice_of``/``_choice_prob`` route on; dict
    criteria pass through untouched.

    ``gate`` — the calling gate's ledger name — is an OPTIONAL third
    parameter the shipped deciders accept (LiveDecider's breaker excludes
    configured gates from its trip math). Implementations MAY omit it;
    gates.py's ``_call`` only passes it to deciders whose signature accepts
    it, so a two-argument ``decide(state, questions)`` remains a valid
    (and common, in tests) implementation.
    """

    def decide(
        self, state: str | dict, questions: dict[str, dict], gate: str | None = None
    ) -> Decision: ...


class NullDecider:
    """Skips every gate: not applicable, deterministic path, zero tokens."""

    def decide(
        self, state: str | dict, questions: dict[str, dict], gate: str | None = None
    ) -> Decision:
        return Decision(applies=False, ok=True, answers={}, raw_tokens=0)


class MockDecider:
    """Scripted verdicts for offline tests.

    Exact question-id match first, then the LONGEST question-id-prefix match
    (so a verdict keyed ``"claim"`` answers question ``"claim_0"``), else
    ``Decision(applies=False)``.
    """

    def __init__(self, verdicts: dict[str, Decision]) -> None:
        self.verdicts = dict(verdicts)

    def decide(
        self, state: str | dict, questions: dict[str, dict], gate: str | None = None
    ) -> Decision:
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
    """Thin Jev client. Failure modes collapse to ``ok=False`` Decisions —
    each carrying ``cause``, the short failure summary the ledger's error
    column now emits (``decision.cause or "jev_error"`` at the write sites).

    Request-boundary normalization (:func:`_wire_questions`): choice
    questions may ship criteria as a list of labels; the wire always
    carries ``{label: ""}`` because TypeSafe's schema requires an object
    (list-shaped criteria 422'd taxonomy typing, posture_audit and routing
    on every call since birth). Callers' question dicts are never mutated.

    Breaker exclusion (``breaker.exclude_gates``): calls whose ``gate`` is on
    the exclusion list do not participate in the trip decision AT ALL —
    absent from the numerator AND the denominator (counting their calls but
    not their errors would dilute the ratio dishonestly). They are still
    fast-failed while the breaker is open: once tripped, NO network chatter
    goes out, whatever the gate — the exclusion only shapes the trip math.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        timeout_s: float = 5.0,
        per_question_s: float = DEFAULT_PER_QUESTION_S,
        max_retries: int = 1,
        error_rate_breaker: float = BREAKER_THRESHOLD,
        breaker_min_calls: int = BREAKER_MIN_CALLS,
        breaker_exclude_gates: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.per_question_s = per_question_s
        self.max_retries = max_retries
        self.error_rate_breaker = error_rate_breaker
        # At least one counted call before the ratio exists (also guards the
        # division in _breaker_tripped).
        self.breaker_min_calls = max(1, int(breaker_min_calls))
        self.breaker_exclude_gates = frozenset(
            str(gate).strip().lower() for gate in (breaker_exclude_gates or [])
        )
        # Error-rate breaker bookkeeping (per instance == per run). A COUNTED
        # call is one per decide() invocation that passes the breaker check
        # AND whose gate is not excluded; it is an error only if the
        # invocation ultimately fails. ``excluded_calls`` is telemetry only.
        self.total_calls = 0
        self.errors = 0
        self.excluded_calls = 0
        self._client = httpx.Client(timeout=timeout_s)

    def close(self) -> None:
        """Release the connection pool (hygiene; not required for correctness)."""
        self._client.close()

    @property
    def breaker_open(self) -> bool:
        """True once the error-rate breaker has tripped (read-only).

        The trip condition is monotonic in practice — errors never decrease
        and no network call goes out while tripped — so once open the breaker
        stays open for the life of the instance.
        """
        return self._breaker_tripped()

    def decide(
        self, state: str | dict, questions: dict[str, dict], gate: str | None = None
    ) -> Decision:
        # Breaker check BEFORE the call: sustained failure stops the network
        # chatter entirely; the minimum sample keeps one transient error from
        # disabling Jev for the rest of the run. Applies to excluded gates
        # too — exclusion shapes the trip MATH, not the open-state protection.
        if self._breaker_tripped():
            logger.debug(
                "jev breaker tripped (errors={}/{}, threshold={}); failing closed",
                self.errors,
                self.total_calls,
                self.error_rate_breaker,
            )
            return Decision(
                applies=True,
                ok=False,
                answers={},
                raw_tokens=0,
                cause=(
                    f"BreakerOpen: {self.errors}/{self.total_calls} errors "
                    f"> {self.error_rate_breaker}"
                ),
            )

        counted = not self._gate_excluded(gate)
        if counted:
            self.total_calls += 1
        else:
            self.excluded_calls += 1
        timeout = effective_timeout(self.timeout_s, self.per_question_s, len(questions or {}))
        # List criteria normalize to {label: ""} at this single boundary
        # (TypeSafe's schema requires an object); see _wire_questions.
        payload = {
            "state": state,
            "model": self.model,
            "questions": _wire_questions(questions),
        }
        # Never logged: the Authorization header / api_key stay out of log args.
        headers = {"Authorization": f"Bearer {self.api_key}"}

        last_cause: str | None = None
        for attempt in range(self.max_retries + 1):
            if attempt > 0:
                _backoff_sleep(random.uniform(0.05, 0.5))
            try:
                resp = self._client.post(
                    self.base_url, json=payload, headers=headers, timeout=timeout
                )
                if resp.status_code >= 500:
                    logger.debug("jev 5xx on attempt {}: {}", attempt + 1, resp.status_code)
                    last_cause = self._http_cause(resp)
                    continue  # retryable
                if resp.status_code >= 400:
                    logger.debug("jev 4xx: {}; no retry", resp.status_code)
                    return self._failed(self._http_cause(resp), counted=counted)
                try:
                    body = resp.json()
                except ValueError as exc:
                    logger.debug("jev malformed JSON body; no retry")
                    return self._failed(exception_cause(exc), counted=counted)
                return self._parse(body, counted=counted)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                logger.debug(
                    "jev transport failure on attempt {}: {}",
                    attempt + 1,
                    type(exc).__name__,
                )
                last_cause = exception_cause(exc)
                continue  # transient network-level failure -> retryable

        return self._failed(last_cause, counted=counted)

    def _gate_excluded(self, gate: str | None) -> bool:
        """True when this call's gate sits on the breaker's exclusion list."""
        return gate is not None and gate.strip().lower() in self.breaker_exclude_gates

    def _breaker_tripped(self) -> bool:
        if self.total_calls < self.breaker_min_calls:
            return False
        return self.errors / self.total_calls > self.error_rate_breaker

    def _failed(self, cause: str | None = None, *, counted: bool = True) -> Decision:
        """Count one failure (unless the call sat in an excluded gate) and
        collapse to the failed Decision carrying the short ``cause``."""
        if counted:
            self.errors += 1
        return Decision(applies=True, ok=False, answers={}, raw_tokens=0, cause=cause)

    @staticmethod
    def _http_cause(resp: httpx.Response) -> str:
        """The HTTP-error cause WITH a snippet of the response body: for a
        4xx the body names the violated constraint (the taxonomy 422s said
        ``Input should be a valid dictionary`` for years — the old generic
        line discarded that). Falls back to the bare status line when the
        response carries no text. Stays SHORT: it lands verbatim in the
        ledger's error column."""
        snippet = (resp.text or "").strip()
        if snippet:
            return f"HTTPStatusError: {resp.status_code}: {snippet[:_HTTP_BODY_SNIPPET_CHARS]}"
        return f"HTTPStatusError: {resp.status_code}"

    def _parse(self, body: object, *, counted: bool = True) -> Decision:
        """Normalize (copy only) a 200 response into a Decision."""
        if not isinstance(body, dict) or not isinstance(body.get("answers"), dict):
            # Valid JSON, wrong shape: the gate could do nothing with it —
            # fail the decision rather than hand back silent emptiness.
            logger.debug("jev response missing answers map; failing the decision")
            return self._failed(
                "MissingAnswers: response carried no answers map", counted=counted
            )
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
        previous = _decider_instance
        if mode_off:
            instance: Decider = NullDecider()
        else:
            api_key = os.environ.get("TYPESAFE_API_KEY")
            consent = os.environ.get("SIGNALS_DECIDE_DATA_EXIT")
            if api_key and consent == DATA_EXIT_CONSENT:
                # Breaker knobs: the structured ``breaker`` block wins when
                # present; the legacy flat ``error_rate_breaker`` key still
                # works alone (pre-block configs keep today's math exactly —
                # min_calls 5, threshold 0.20, nothing excluded).
                breaker_cfg = decider_cfg.get("breaker") or {}
                try:
                    breaker_threshold = float(
                        breaker_cfg.get(
                            "threshold", decider_cfg.get("error_rate_breaker", BREAKER_THRESHOLD)
                        )
                    )
                except (TypeError, ValueError):
                    logger.warning(
                        "decide layer: bad breaker threshold {!r}; using default {}",
                        breaker_cfg.get("threshold", decider_cfg.get("error_rate_breaker")),
                        BREAKER_THRESHOLD,
                    )
                    breaker_threshold = BREAKER_THRESHOLD
                try:
                    breaker_min_calls = int(
                        breaker_cfg.get("min_calls", BREAKER_MIN_CALLS)
                    )
                except (TypeError, ValueError):
                    logger.warning(
                        "decide layer: bad breaker min_calls {!r}; using default {}",
                        breaker_cfg.get("min_calls"),
                        BREAKER_MIN_CALLS,
                    )
                    breaker_min_calls = BREAKER_MIN_CALLS
                raw_excluded = breaker_cfg.get("exclude_gates") or []
                if isinstance(raw_excluded, str):
                    raw_excluded = [raw_excluded]  # a single gate name, tolerated
                exclude_gates = tuple(str(gate) for gate in raw_excluded)
                instance = LiveDecider(
                    base_url=base_url,
                    model=model,
                    api_key=api_key,
                    timeout_s=float(decider_cfg.get("timeout_s", 5.0)),
                    per_question_s=float(
                        decider_cfg.get("per_question_s", DEFAULT_PER_QUESTION_S)
                    ),
                    max_retries=int(decider_cfg.get("max_retries", 1)),
                    error_rate_breaker=breaker_threshold,
                    breaker_min_calls=breaker_min_calls,
                    breaker_exclude_gates=exclude_gates,
                )
            else:
                logger.debug(
                    "decide layer on but no live decider: missing TYPESAFE_API_KEY "
                    "or SIGNALS_DECIDE_DATA_EXIT consent -> NullDecider"
                )
                instance = NullDecider()
        _decider_instance = instance
        _decider_key = key
        if previous is not None and previous is not instance:
            # A swapped-out decider must not leak its httpx connection pool.
            try:
                getattr(previous, "close", lambda: None)()
            except Exception as exc:  # hygiene only; never fail the swap
                logger.debug("decider swap: closing previous instance failed: {}", exc)
        return instance
