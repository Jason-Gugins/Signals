"""Tests for the Jev decider seam (src/decide/).

Offline: every HTTP interaction goes through respx; the only test that talks
to a real endpoint is the llm_live ping, which self-skips without
TYPESAFE_API_KEY. Env vars (key + data-exit consent) are monkeypatched
explicitly — no test depends on the developer's real .env.
"""

from __future__ import annotations

import json
import os
import time

import httpx
import pytest
import respx

import src.decide.jev as jev
from src.decide import (
    Claim,
    Decision,
    Decider,
    LiveDecider,
    MockDecider,
    NullDecider,
    Plan,
    PlanStep,
    get_decider,
)

BASE = "https://api.typesafe.ai/v1/systemone"


def _noul_q() -> dict:
    return {"type": "noul", "instructions": "Score on-topic-ness."}


def _ok_body() -> dict:
    """A minimal valid Jev response (fresh dict per call — no shared mutation)."""
    return {
        "answers": {"q1": {"noul": 0.91}},
        "usage": {"input_tokens": 42, "output_tokens": 7},
    }


def test_null_decider_skips_all_gates():
    d = NullDecider().decide("state payload", {"g1": _noul_q()})
    assert d.applies is False
    assert d.ok is True
    assert d.answers == {}
    assert d.raw_tokens == 0


def test_mock_decider_returns_scripted_verdicts():
    yes = Decision(applies=True, ok=True, answers={"claim_0": {"noul": 0.9}}, raw_tokens=7)
    fail = Decision(applies=True, ok=False)
    mock = MockDecider({"claim_0": yes, "claim": fail, "g1": yes})

    # Exact question-id match wins over any prefix.
    assert mock.decide("state", {"claim_0": _noul_q()}) is yes
    # Prefix match: "claim_5" falls back to the "claim" verdict.
    assert mock.decide("state", {"claim_5": _noul_q()}) is fail
    # Longest prefix wins when several keys match.
    long = Decision(applies=True, ok=True, raw_tokens=1)
    mock2 = MockDecider({"claim": fail, "claim_5": long})
    assert mock2.decide("state", {"claim_5_sub": _noul_q()}) is long
    # Unmatched id -> not applicable (deterministic path).
    d = mock.decide("state", {"other": {"type": "choice", "instructions": "pick"}})
    assert d.applies is False
    assert d.ok is True


@respx.mock
def test_live_decider_request_shape():
    route = respx.post(BASE).mock(
        return_value=httpx.Response(200, json=_ok_body())
    )
    decider = LiveDecider(
        base_url=BASE, model="jev-1.13.0", api_key="test-dummy", timeout_s=2.5, max_retries=0
    )
    assert decider.timeout_s == 2.5  # constructor accepts + stores the timeout

    decision = decider.decide(
        "state text",
        {
            "q1": _noul_q(),
            "q2": {"type": "choice", "instructions": "Pick a lane.", "criteria": ["deep", "quick"]},
        },
    )
    assert decision.ok is True
    assert route.called
    request = route.calls.last.request
    body = json.loads(request.content)
    assert body["state"] == "state text"
    assert body["model"] == "jev-1.13.0"
    assert set(body["questions"]) == {"q1", "q2"}
    assert body["questions"]["q1"]["type"] == "noul"
    assert body["questions"]["q1"]["instructions"] == "Score on-topic-ness."
    assert body["questions"]["q2"]["criteria"] == ["deep", "quick"]
    # Bearer auth header present (dummy value in tests; never logged anywhere).
    assert request.headers["Authorization"] == "Bearer test-dummy"


@respx.mock
def test_live_decider_parses_answers():
    respx.post(BASE).mock(
        return_value=httpx.Response(
            200,
            json={
                "answers": {
                    "route_1": {
                        "choice": "deep",
                        "probabilities": {"deep": 0.7, "quick": 0.3},
                        "confidence": 0.82,
                    },
                    "score_1": {
                        "score": 0.75,
                        "legend": ["not relevant", "relevant"],
                        "probabilities": [0.25, 0.75],
                        "confidence": 0.9,
                    },
                    "claim_0": {"noul": 0.88},
                },
                "usage": {"input_tokens": 1234, "output_tokens": 56},
            },
        )
    )
    decider = LiveDecider(base_url=BASE, model="jev-1.13.0", api_key="test-dummy", max_retries=0)
    decision = decider.decide(
        {"domain": "acme.com", "claim_0": "anchor window text"},
        {
            "route_1": {"type": "choice", "instructions": "Route.", "criteria": ["deep", "quick"]},
            "score_1": {"type": "score", "instructions": "Score.", "criteria": ["no", "yes"]},
            "claim_0": _noul_q(),
        },
    )
    assert decision.applies is True
    assert decision.ok is True
    # choice answer
    assert decision.answers["route_1"]["choice"] == "deep"
    assert decision.answers["route_1"]["probabilities"] == {"deep": 0.7, "quick": 0.3}
    assert decision.answers["route_1"]["confidence"] == 0.82
    # score answer
    assert decision.answers["score_1"]["score"] == 0.75
    assert decision.answers["score_1"]["legend"] == ["not relevant", "relevant"]
    # noul answer
    assert decision.answers["claim_0"]["noul"] == 0.88
    # Jev bills input tokens only; raw_tokens mirrors usage.input_tokens.
    assert decision.raw_tokens == 1234


@respx.mock
def test_live_decider_error_is_decision_not_exception():
    """HTTP error, connection error, malformed JSON -> Decision(applies=True, ok=False)."""
    behavior = {"mode": "http_500"}

    def handler(request):
        mode = behavior["mode"]
        if mode == "http_500":
            return httpx.Response(500)
        if mode == "connect_error":
            raise httpx.ConnectError("connection refused")
        # Valid HTTP but not JSON at all.
        return httpx.Response(
            200, content=b"<html>gateway error</html>", headers={"content-type": "text/html"}
        )

    respx.post(BASE).mock(side_effect=handler)
    decider = LiveDecider(base_url=BASE, model="jev-1.13.0", api_key="test-dummy", max_retries=0)

    for mode in ("http_500", "connect_error", "bad_json"):
        behavior["mode"] = mode
        d = decider.decide("state", {"q1": _noul_q()})
        assert d.applies is True, mode
        assert d.ok is False, mode
        assert d.answers == {}, mode
        assert d.raw_tokens == 0, mode


@respx.mock
def test_retry_and_breaker(monkeypatch):
    monkeypatch.setattr(jev, "_backoff_sleep", lambda _seconds: None)

    # (a) 5xx then retry succeeds -> ok=True.
    route = respx.post(BASE).mock(
        side_effect=[httpx.Response(500), httpx.Response(200, json=_ok_body())]
    )
    decider = LiveDecider(base_url=BASE, model="jev-1.13.0", api_key="test-dummy", max_retries=1)
    decision = decider.decide("state", {"q1": _noul_q()})
    assert decision.applies is True
    assert decision.ok is True
    assert decision.answers["q1"]["noul"] == 0.91
    assert decision.raw_tokens == 42
    assert route.call_count == 2  # one retry

    # (b) 5xx on both attempts -> failed Decision, no exception.
    respx.post(BASE).mock(side_effect=[httpx.Response(500), httpx.Response(500)])
    decider = LiveDecider(base_url=BASE, model="jev-1.13.0", api_key="test-dummy", max_retries=1)
    decision = decider.decide("state", {"q1": _noul_q()})
    assert decision.applies is True
    assert decision.ok is False
    assert decision.answers == {}

    # (c) Error-rate breaker: 2 of the first 5 calls error -> the 6th call
    # fails closed WITHOUT touching the network.
    counter = {"n": 0}

    def handler(request):
        counter["n"] += 1
        if counter["n"] <= 2:
            return httpx.Response(500)
        return httpx.Response(200, json=_ok_body())

    route = respx.post(BASE).mock(side_effect=handler)
    decider = LiveDecider(
        base_url=BASE, model="jev-1.13.0", api_key="test-dummy", max_retries=0
    )
    assert decider.breaker_open is False  # fresh instance: breaker closed
    for _ in range(5):
        decider.decide("state", {"q1": _noul_q()})
    assert decider.total_calls == 5
    assert decider.errors == 2  # 2/5 = 0.40 > breaker threshold 0.20
    assert decider.breaker_open is True  # tripped and visible
    before = route.call_count
    d6 = decider.decide("state", {"q1": _noul_q()})
    assert d6.applies is True
    assert d6.ok is False
    assert route.call_count == before  # breaker trip made no network call
    assert decider.breaker_open is True  # stays open (no call went out)


def _decider_cfg(mode: str) -> dict:
    return {"mode": mode, "decider": {"model": "jev-1.13.0"}}


def test_get_decider_factory_gates(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("SIGNALS_DECIDE_DATA_EXIT", raising=False)

    # No key -> NullDecider, even in enforce mode.
    jev._reset_decider_cache()
    assert isinstance(get_decider(_decider_cfg("enforce")), NullDecider)

    # Key but missing consent -> NullDecider.
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-dummy")
    jev._reset_decider_cache()
    assert isinstance(get_decider(_decider_cfg("enforce")), NullDecider)

    # Wrong consent string -> NullDecider (exact string required).
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", "yes I understand")
    jev._reset_decider_cache()
    assert isinstance(get_decider(_decider_cfg("enforce")), NullDecider)

    # Key + exact consent + enforce -> LiveDecider with model/base_url from cfg.
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", jev.DATA_EXIT_CONSENT)
    jev._reset_decider_cache()
    cfg = {
        "mode": "enforce",
        "decider": {"model": "jev-test-9", "base_url": "https://api.example.test/v1"},
    }
    d = get_decider(cfg)
    assert isinstance(d, LiveDecider)
    assert isinstance(d, Decider)  # runtime-checkable protocol
    assert d.model == "jev-test-9"
    assert d.base_url == "https://api.example.test/v1"
    # Memoized: same (mode, model, base_url) -> same instance.
    assert get_decider(dict(cfg)) is d

    # mode off -> NullDecider regardless of keys/consent.
    jev._reset_decider_cache()
    assert isinstance(
        get_decider({"mode": "off", "decider": {"model": "jev-test-9"}}), NullDecider
    )

    # shadow + keys -> live as well (shadow still calls Jev, logs only).
    jev._reset_decider_cache()
    assert isinstance(
        get_decider({"mode": "shadow", "decider": {"model": "jev-test-9"}}), LiveDecider
    )
    jev._reset_decider_cache()


def test_get_decider_closes_previous_instance_on_swap(monkeypatch):
    """Swapping configs must not leak the old LiveDecider's httpx pool."""
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-dummy")
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", jev.DATA_EXIT_CONSENT)
    jev._reset_decider_cache()
    cfg_a = {"mode": "enforce", "decider": {"model": "jev-swap-a"}}
    cfg_b = {"mode": "enforce", "decider": {"model": "jev-swap-b"}}
    try:
        first = get_decider(cfg_a)
        assert isinstance(first, LiveDecider)
        assert first._client.is_closed is False
        second = get_decider(cfg_b)  # different key -> new instance
        assert second is not first
        assert isinstance(second, LiveDecider)
        assert first._client.is_closed is True  # previous client released
        assert second._client.is_closed is False
        # Same key -> memoized instance, nothing closed.
        assert get_decider(dict(cfg_b)) is second
        assert second._client.is_closed is False
    finally:
        jev._reset_decider_cache()
        second.close()


@pytest.mark.llm_live
# live_fetch: conftest's network gate only opens for known markers; this test
# genuinely hits the TypeSafe endpoint when run opt-in (-m llm_live with keys).
@pytest.mark.live_fetch
@pytest.mark.skipif(
    not os.environ.get("TYPESAFE_API_KEY"), reason="TYPESAFE_API_KEY not set; live Jev ping skipped"
)
def test_live_jev_ping():
    decider = LiveDecider(
        base_url=BASE,
        model="jev-1.13.0",
        api_key=os.environ["TYPESAFE_API_KEY"],
        timeout_s=25.0,
        max_retries=0,
    )
    start = time.monotonic()
    decision = decider.decide(
        "Signals llm_live smoke ping: a one-line state payload for the Jev decider.",
        {
            "ping": {
                "type": "noul",
                "instructions": "Does this state describe a real system ping? Answer with a noul score.",
            }
        },
    )
    elapsed = time.monotonic() - start
    # Shape assertions only — the real noul distribution is tuned downstream.
    assert decision.applies is True
    assert decision.ok is True, f"live Jev ping failed: {decision!r}"
    assert "ping" in decision.answers
    noul = decision.answers["ping"].get("noul")
    assert isinstance(noul, (int, float)) and 0.0 <= float(noul) <= 1.0
    assert decision.raw_tokens >= 0
    assert elapsed < 30


# --- Task 5 (fix wave 2026-10-12): causes, breaker exclusion, timeout scaling ---

def test_exception_cause_format():
    from src.decide.shapes import exception_cause

    assert exception_cause(ValueError("boom")) == "ValueError: boom"
    assert len(exception_cause(ValueError("x" * 500))) <= len("ValueError: ") + 120


def test_decision_cause_defaults_none():
    d = Decision(applies=True, ok=True, answers={}, raw_tokens=1)
    assert d.cause is None


def test_effective_timeout_formula():
    # base + 0.5 x max(0, n - 3)
    assert jev.effective_timeout(5.0, 0.5, 3) == 5.0
    assert jev.effective_timeout(5.0, 0.5, 1) == 5.0
    assert jev.effective_timeout(5.0, 0.5, 10) == pytest.approx(8.5)
    assert jev.effective_timeout(5.0, 0.5, 25) == pytest.approx(16.0)
    assert jev.effective_timeout(5.0, 0.0, 100) == 5.0


@respx.mock
def test_failure_decision_carries_cause(monkeypatch):
    monkeypatch.setattr(jev, "_backoff_sleep", lambda _s: None)
    behavior = {"mode": "http_500"}

    def handler(request):
        if behavior["mode"] == "http_500":
            return httpx.Response(500)
        raise httpx.ConnectError("connection refused")

    respx.post(BASE).mock(side_effect=handler)
    decider = LiveDecider(base_url=BASE, model="jev-1.13.0", api_key="k", max_retries=0)

    d = decider.decide("state", {"q1": _noul_q()})
    assert d.ok is False and d.cause and "HTTPStatusError" in d.cause

    behavior["mode"] = "connect_error"
    d = decider.decide("state", {"q1": _noul_q()})
    assert d.ok is False and d.cause and "ConnectError" in d.cause


@respx.mock
def test_breaker_excludes_configured_gates_from_trip_math(monkeypatch):
    """The A/B's helcim mechanism: chronic minor-gate failures must not
    fail-close G4. Excluded-gate calls sit OUT of numerator AND denominator."""
    monkeypatch.setattr(jev, "_backoff_sleep", lambda _s: None)
    respx.post(BASE).mock(return_value=httpx.Response(500))
    decider = LiveDecider(
        base_url=BASE, model="jev-1.13.0", api_key="k", max_retries=0,
        error_rate_breaker=0.5, breaker_min_calls=5,
        breaker_exclude_gates=["routing", "posture_audit"],
    )
    for _ in range(6):
        d = decider.decide("s", {"q1": _noul_q()}, gate="routing")
        assert d.ok is False
    # every call excluded: no counted sample at all, breaker never had data
    assert decider.total_calls == 0
    assert decider.excluded_calls == 6
    assert decider.breaker_open is False
    # a counted gate still participates: 1 counted call < min 5 => not open
    decider.decide("s", {"q1": _noul_q()}, gate="citation_soundness")
    assert decider.total_calls == 1
    assert decider.breaker_open is False


@respx.mock
def test_breaker_trips_on_counted_failures_and_fast_fails_everything(monkeypatch):
    monkeypatch.setattr(jev, "_backoff_sleep", lambda _s: None)
    respx.post(BASE).mock(return_value=httpx.Response(500))
    decider = LiveDecider(
        base_url=BASE, model="jev-1.13.0", api_key="k", max_retries=0,
        error_rate_breaker=0.5, breaker_min_calls=2,
    )
    assert decider.decide("s", {"q1": _noul_q()}, gate="citation_soundness").ok is False
    assert decider.decide("s", {"q1": _noul_q()}, gate="citation_soundness").ok is False
    assert decider.breaker_open is True  # 2/2 counted errors > 0.5
    # while open, even an EXCLUDED gate fast-fails with an honest cause
    d = decider.decide("s", {"q1": _noul_q()}, gate="routing")
    assert d.ok is False
    assert d.cause and d.cause.startswith("BreakerOpen")


def test_call_threads_gate_only_to_deciders_that_accept_it():
    """Two-argument deciders (every test fake, third-party impls) keep
    working; three-argument deciders receive the gate for breaker math."""
    from src.decide.gates import DecideLedger, _call

    class TwoArg:
        def decide(self, state, questions):
            return Decision(applies=True, ok=True, answers={"q": {}}, raw_tokens=1)

    class ThreeArg:
        def __init__(self):
            self.seen_gate = "unset"

        def decide(self, state, questions, gate=None):
            self.seen_gate = gate
            return Decision(applies=True, ok=True, answers={"q": {}}, raw_tokens=1)

    two, three = TwoArg(), ThreeArg()
    d1, _ = _call(two, "s", {"q": _noul_q()}, gate="routing")
    assert d1.ok is True  # no TypeError: gate silently not passed
    d2, _ = _call(three, "s", {"q": _noul_q()}, gate="routing")
    assert d2.ok is True and three.seen_gate == "routing"


def test_gate_error_row_emits_decision_cause():
    """A gate row's error column names the real cause (decision.cause), not
    the legacy bare 'jev_error'."""
    from src.decide.gates import DecideLedger, gate_citation_batch

    class FailingDecider:
        def decide(self, state, questions, gate=None):
            return Decision(
                applies=True, ok=False, answers={}, raw_tokens=0,
                cause="ConnectError: connection refused",
            )

    ledger = DecideLedger()
    doc_text = "acme raised a 40m series b round to expand its inference platform"
    claim = Claim("acme raised a 40m series b round", "doc1")
    gate_citation_batch([claim], "doc1", doc_text, FailingDecider(), {}, ledger, "run-1", "enforce")
    row = ledger.rows[-1]
    # the contract under test: the error COLUMN names the real cause (the
    # outcome label follows the gate's on_error semantics, not this test)
    assert row["error"] == "ConnectError: connection refused"


def test_get_decider_reads_structured_breaker_block(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-dummy")
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", jev.DATA_EXIT_CONSENT)
    try:
        live = get_decider({
            "mode": "shadow",
            "decider": {
                "model": "jev-blocked",
                "timeout_s": 4.0,
                "per_question_s": 0.75,
                "breaker": {"min_calls": 7, "threshold": 0.25, "exclude_gates": ["routing"]},
            },
        })
        assert isinstance(live, LiveDecider)
        assert live.breaker_min_calls == 7
        assert live.error_rate_breaker == 0.25
        assert live.breaker_exclude_gates == frozenset({"routing"})
        assert live.per_question_s == 0.75
        # legacy flat key still honored when the block is absent (distinct
        # model so the (mode, model, base_url) memo cannot alias the calls)
        legacy = get_decider({"mode": "shadow", "decider": {"model": "jev-legacy", "error_rate_breaker": 0.4}})
        assert isinstance(legacy, LiveDecider) and legacy.error_rate_breaker == 0.4
    finally:
        get_decider({"mode": "off"})  # close the live instances
