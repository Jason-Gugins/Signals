"""Tests for the implementer sub-stage (src/llm/implement.py).

Fully offline: every HTTP interaction goes through respx, gating uses
MockDecider/CountingDecider (no network), and no test spends a token.

Covers:
- build_extraction_messages contract (render_prompt mirror, quick/deep caps)
- call_implementer request shape (OpenRouter nested ``reasoning`` vs the
  native ``reasoning_effort`` param, Bearer header, temperature 0) and the
  never-raises failure seam (500 / malformed content / non-JSON body)
- parse_claims atomization (preserve hallucinated citations, batch fallback,
  caps)
- screen_and_gate_claims: a foreign doc_id is dropped BEFORE any Jev call;
  survivors flow through gate_citation_batch with the noul attached
- extract_five_fields + five_fields_to_claims (the G4-gated dossier fields)
- claims_to_candidates: text-hash natural_key (order-independent),
  confidence from noul, evidence_data provenance, observed_at = fetched_at
- END-TO-END through normalize_batch with the registered ``llm_need`` type
- implement_batch: the serial v1 contract (no nesting, input order)
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading

import httpx
import respx

from src.core.models import Account
from src.decide import DecideLedger, MockDecider
from src.decide.shapes import Claim, Decision
from src.llm.implement import (
    MAX_CLAIMS_DEEP,
    build_extraction_messages,
    call_implementer,
    claims_to_candidates,
    extract_five_fields,
    five_fields_to_claims,
    implement_batch,
    parse_claims,
    screen_and_gate_claims,
)
from src.signals.normalize import SignalCandidate, normalize_batch
from src.signals.taxonomy import Taxonomy

BASE_URL = "https://openrouter.ai/api/v1"
CHAT_URL = f"{BASE_URL}/chat/completions"
NATIVE_BASE_URL = "https://api.z.ai/api/paas/v4"
NATIVE_CHAT_URL = f"{NATIVE_BASE_URL}/chat/completions"

RUN = "run-implement-test"
DOC_ID = "doc-1"
DOC_TEXT = (
    "Company blog. "
    "Acme is evaluating SIEM vendors after a breach last quarter. "
    "The team plans to consolidate three monitoring tools into one platform. "
    "Footer navigation."
)
GOOD_1 = "Acme is evaluating SIEM vendors after a breach last quarter"
GOOD_2 = "The team plans to consolidate three monitoring tools into one platform"
# Overlapping vocabulary but a FOREIGN doc_id: only the doc_id screen can
# catch it (the lexical prefilter would pass it), so a zero-Jev-call run
# proves the screen fired BEFORE the gate.
FOREIGN = "Acme announced a completely unrelated blockchain initiative"
FIVE_FIELDS_OK = {
    "operational_need": {"text": "Needs SOC2 aligned detection", "doc_id": DOC_ID},
    "buying_window": {"text": "Contract renews in Q1", "doc_id": DOC_ID},
    "displacement_risk": "unknown",
    "expansion_signal": {"text": "unknown"},
    "why_now": {"text": "Breach drives urgency", "doc_id": DOC_ID},
}


class CountingDecider(MockDecider):
    """MockDecider that records every decide() invocation for inspection."""

    def __init__(self, verdicts: dict[str, Decision]) -> None:
        super().__init__(verdicts)
        self.calls: list[tuple[object, dict]] = []

    @property
    def n_calls(self) -> int:
        return len(self.calls)

    def decide(self, state: str | dict, questions: dict[str, dict]) -> Decision:
        self.calls.append((copy.deepcopy(state), copy.deepcopy(questions)))
        return super().decide(state, questions)


def _batch_decider(answers: dict[str, dict], tokens: int = 120) -> MockDecider:
    """One batched G4 verdict whose answers map carries per-claim noul values
    (the gate reads ``answers["claim_<i>"]`` for the questions it asked)."""
    return MockDecider(
        {"claim": Decision(applies=True, ok=True, answers=answers, raw_tokens=tokens)}
    )


def _screen(claims, decider, *, doc_text=DOC_TEXT, mode="enforce", gates_cfg=None) -> DecideLedger:
    ledger = DecideLedger()
    screen_and_gate_claims(
        claims,
        batch_doc_id=DOC_ID,
        doc_text=doc_text,
        decider=decider,
        gates_cfg=gates_cfg or {},
        ledger=ledger,
        run_id=RUN,
        mode=mode,
    )
    return ledger


# ------------------------------------------------- build_extraction_messages


def test_build_extraction_messages_deep_contract():
    messages = build_extraction_messages(DOC_ID, DOC_TEXT)
    assert [m["role"] for m in messages] == ["system", "user"]
    system = messages[0]["content"]
    # The render_prompt contract, mirrored verbatim for the document seam.
    assert "Use ONLY the supplied document." in system
    assert "Every claim must carry the document's doc_id." in system
    assert "If the document does not support a claim, do not emit it." in system
    # STRICT JSON output shape is named.
    for token in ('"claims"', '"text"', '"doc_id"'):
        assert token in system
    # deep route states the deep cap.
    assert str(MAX_CLAIMS_DEEP) in system
    user = messages[1]["content"]
    assert DOC_ID in user
    assert DOC_TEXT in user


def test_build_extraction_messages_quick_route_shortens_and_caps_at_five():
    deep = build_extraction_messages(DOC_ID, DOC_TEXT, route="deep")
    quick = build_extraction_messages(DOC_ID, DOC_TEXT, route="quick")
    assert "5" in quick[0]["content"]  # MAX_CLAIMS_QUICK cap is stated
    assert len(quick[0]["content"]) < len(deep[0]["content"])


def test_build_extraction_messages_includes_doc_meta():
    meta = {"title": "Q3 security post", "url": "https://acme.test/blog/q3"}
    user = build_extraction_messages(DOC_ID, DOC_TEXT, meta)[1]["content"]
    assert "Q3 security post" in user
    assert "https://acme.test/blog/q3" in user


# ------------------------------------------------------------ call_implementer


@respx.mock
def test_call_implementer_openrouter_reasoning_param_and_bearer():
    route = respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps({"claims": []})}}]}
        )
    )
    messages = [{"role": "user", "content": "extract"}]
    raw = call_implementer(
        messages,
        model="deepseek/deepseek-v4.1-flash",
        base_url=BASE_URL,
        api_key="or-dummy",
        reasoning_effort="max",
    )
    assert raw == {"claims": []}
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Bearer or-dummy"
    body = json.loads(req.content)
    assert body["model"] == "deepseek/deepseek-v4.1-flash"
    assert body["messages"] == messages
    assert body["temperature"] == 0
    # OpenRouter takes the UNIFIED nested form...
    assert body["reasoning"] == {"effort": "max"}
    # ...and NOT the standalone native parameter.
    assert "reasoning_effort" not in body


@respx.mock
def test_call_implementer_native_base_url_uses_reasoning_effort():
    route = respx.post(NATIVE_CHAT_URL).mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps({"claims": []})}}]}
        )
    )
    call_implementer(
        [{"role": "user", "content": "extract"}],
        model="z-ai/glm-5.3-flash",
        base_url=NATIVE_BASE_URL,
        api_key="z-dummy",
        reasoning_effort="max",
    )
    body = json.loads(route.calls.last.request.content)
    assert body["reasoning_effort"] == "max"
    assert "reasoning" not in body


@respx.mock
def test_call_implementer_omits_reasoning_keys_when_effort_is_none():
    route = respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps({"claims": []})}}]}
        )
    )
    call_implementer(
        [{"role": "user", "content": "extract"}],
        model="m",
        base_url=BASE_URL,
        api_key="k",
        reasoning_effort=None,
    )
    body = json.loads(route.calls.last.request.content)
    assert "reasoning" not in body
    assert "reasoning_effort" not in body


@respx.mock
def test_call_implementer_strips_json_fence():
    fenced = "```json\n" + json.dumps({"claims": []}) + "\n```"
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": fenced}}]})
    )
    assert (
        call_implementer([{"role": "user", "content": "x"}], model="m", base_url=BASE_URL, api_key="k")
        == {"claims": []}
    )


@respx.mock
def test_call_implementer_http_500_returns_none():
    respx.post(CHAT_URL).mock(return_value=httpx.Response(500, json={"error": "boom"}))
    assert (
        call_implementer([{"role": "user", "content": "x"}], model="m", base_url=BASE_URL, api_key="k")
        is None
    )


@respx.mock
def test_call_implementer_malformed_json_content_returns_none():
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": "I cannot emit JSON today"}}]}
        )
    )
    assert (
        call_implementer([{"role": "user", "content": "x"}], model="m", base_url=BASE_URL, api_key="k")
        is None
    )


@respx.mock
def test_call_implementer_non_json_body_returns_none():
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, text="<html>gateway oops</html>")
    )
    assert (
        call_implementer([{"role": "user", "content": "x"}], model="m", base_url=BASE_URL, api_key="k")
        is None
    )


@respx.mock
def test_call_implementer_non_dict_json_content_returns_none():
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps([1, 2, 3])}}]}
        )
    )
    assert (
        call_implementer([{"role": "user", "content": "x"}], model="m", base_url=BASE_URL, api_key="k")
        is None
    )


# ----------------------------------------------------------------- parse_claims


def test_parse_claims_preserves_llm_doc_id_and_leaves_evidence_id_none():
    raw = {
        "claims": [
            {"text": GOOD_1, "doc_id": DOC_ID},
            # A hallucinated citation is PRESERVED, not silently rewritten:
            # screen_and_gate_claims drops it downstream where the ledger sees it.
            {"text": GOOD_2, "doc_id": "doc-hallucinated"},
        ]
    }
    claims = parse_claims(raw, batch_doc_id=DOC_ID)
    assert [(c.text, c.doc_id, c.evidence_id) for c in claims] == [
        (GOOD_1, DOC_ID, None),
        (GOOD_2, "doc-hallucinated", None),
    ]


def test_parse_claims_falls_back_to_batch_doc_id():
    raw = {"claims": [{"text": GOOD_1}, {"text": GOOD_2, "doc_id": 42}]}
    claims = parse_claims(raw, batch_doc_id=DOC_ID)
    assert [c.doc_id for c in claims] == [DOC_ID, DOC_ID]


def test_parse_claims_skips_empty_text_and_non_dict_entries():
    raw = {
        "claims": [
            {"text": GOOD_1, "doc_id": DOC_ID},
            {"text": "", "doc_id": DOC_ID},
            {"text": "   ", "doc_id": DOC_ID},
            {"doc_id": DOC_ID},  # no text at all
            "not a dict",
        ]
    }
    claims = parse_claims(raw, batch_doc_id=DOC_ID)
    assert [c.text for c in claims] == [GOOD_1]


def test_parse_claims_caps_at_max_claims_deep():
    raw = {"claims": [{"text": f"claim number {i}", "doc_id": DOC_ID} for i in range(MAX_CLAIMS_DEEP + 5)]}
    claims = parse_claims(raw, batch_doc_id=DOC_ID)
    assert len(claims) == MAX_CLAIMS_DEEP
    assert [c.text for c in claims] == [f"claim number {i}" for i in range(MAX_CLAIMS_DEEP)]


def test_parse_claims_none_or_malformed_returns_empty():
    for raw in (None, "nope", 42, {}, {"claims": "two"}, {"claims": [None, 7]}):
        assert parse_claims(raw, batch_doc_id=DOC_ID) == []


# -------------------------------------------------------- screen_and_gate_claims


def test_screen_and_gate_claims_foreign_doc_id_dropped_before_jev():
    # The foreign claim shares vocabulary with the document (the lexical
    # prefilter alone would NOT drop it) — zero Jev calls proves the
    # deterministic doc_id screen fired first.
    decider = CountingDecider({"claim": Decision(applies=True, ok=True, answers={}, raw_tokens=0)})
    ledger = _screen([Claim(FOREIGN, "doc-999")], decider)
    assert decider.n_calls == 0
    row = ledger.rows[0]
    assert row["gate"] == "citation_soundness"
    assert row["reason"] == "doc_id_mismatch"
    assert row["outcome"] == "dropped"
    assert row["called"] is False
    # The mismatch lands in the aggregate detail (audit value).
    detail = ledger.aggregate("citation_soundness")["detail"]
    assert len(detail) == 1 and detail[0]["reason"] == "doc_id_mismatch"


def test_screen_and_gate_claims_survivors_flow_through_gate_citation_batch():
    # Survivors flow through gate_citation_batch: the above-floor claim comes
    # back ACCEPTED with its noul attached; the below-floor claim is dropped.
    decider = CountingDecider(
        {"claim": Decision(applies=True, ok=True, answers={"claim_0": {"noul": 0.9}, "claim_1": {"noul": 0.3}}, raw_tokens=120)}
    )
    ledger = DecideLedger()
    pairs = screen_and_gate_claims(
        [Claim(GOOD_1, DOC_ID), Claim(GOOD_2, DOC_ID)],
        batch_doc_id=DOC_ID,
        doc_text=DOC_TEXT,
        decider=decider,
        gates_cfg={},
        ledger=ledger,
        run_id=RUN,
        mode="enforce",
    )
    # Only the above-floor claim survives, with its noul attached.
    assert [(c.text, noul) for c, noul in pairs] == [(GOOD_1, 0.9)]
    # ONE batched Jev call for the whole document batch.
    assert decider.n_calls == 1
    outcomes = {row.get("reason"): row["outcome"] for row in ledger.rows if row.get("outcome")}
    assert outcomes[None] == "accepted"
    assert outcomes["floor"] == "dropped"


def test_screen_and_gate_claims_missing_document_drops_all_before_jev():
    decider = CountingDecider({"claim": Decision(applies=True, ok=True, answers={}, raw_tokens=0)})
    ledger = _screen([Claim(GOOD_1, DOC_ID)], decider, doc_text=None)
    assert decider.n_calls == 0
    assert ledger.rows[0]["reason"] == "no_document"


def test_screen_and_gate_claims_shadow_keeps_survivors_and_harvests_missing_noul_as_none():
    # Shadow: the gate is inert, all survivors come back; an answer without a
    # noul value harvests as None (candidates then use the documented default).
    decider = CountingDecider({"claim": Decision(applies=True, ok=True, answers={"claim_0": {}}, raw_tokens=10)})
    ledger = _screen([Claim(GOOD_1, DOC_ID)], decider, mode="shadow")
    pairs = screen_and_gate_claims(
        [Claim(GOOD_1, DOC_ID)],
        batch_doc_id=DOC_ID,
        doc_text=DOC_TEXT,
        decider=decider,
        gates_cfg={},
        ledger=ledger,
        run_id=RUN,
        mode="shadow",
    )
    assert [(c.text, noul) for c, noul in pairs] == [(GOOD_1, None)]


# ------------------------------------------------------------ extract_five_fields


@respx.mock
def test_extract_five_fields_parses_payload_and_sends_max_reasoning():
    route = respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(FIVE_FIELDS_OK)}}]}
        )
    )
    raw = extract_five_fields(
        DOC_ID,
        DOC_TEXT,
        model="z-ai/glm-5.3-flash",
        base_url=BASE_URL,
        api_key="or-dummy",
    )
    assert raw == FIVE_FIELDS_OK
    body = json.loads(route.calls.last.request.content)
    # The reasoning implementer defaults to effort=max on OpenRouter.
    assert body["reasoning"] == {"effort": "max"}
    system = body["messages"][0]["content"]
    assert "Use ONLY the supplied document." in system
    for field in ("operational_need", "buying_window", "displacement_risk", "expansion_signal", "why_now"):
        assert field in system
    assert "unknown" in system  # the per-field fallback is part of the contract
    assert DOC_ID in body["messages"][1]["content"]


@respx.mock
def test_extract_five_fields_http_error_returns_none():
    respx.post(CHAT_URL).mock(return_value=httpx.Response(500, json={"error": "boom"}))
    assert (
        extract_five_fields(DOC_ID, DOC_TEXT, model="m", base_url=BASE_URL, api_key="k")
        is None
    )


def test_five_fields_to_claims_maps_populated_and_skips_unknown():
    fields = {
        "operational_need": {"text": "Needs SOC2 aligned detection", "doc_id": "doc-7"},
        "buying_window": {"text": "Contract renews in Q1"},  # no doc_id -> batch fallback
        "displacement_risk": "unknown",  # bare string form
        "expansion_signal": {"text": "unknown"},  # {"text": "unknown"} form
        "why_now": {"text": "   "},  # blank text
    }
    claims = five_fields_to_claims(fields, batch_doc_id=DOC_ID)
    assert [(c.text, c.doc_id) for c in claims] == [
        ("Needs SOC2 aligned detection", "doc-7"),
        ("Contract renews in Q1", DOC_ID),
    ]
    assert all(c.evidence_id is None for c in claims)


def test_five_fields_to_claims_none_or_malformed_returns_empty():
    assert five_fields_to_claims(None, batch_doc_id=DOC_ID) == []
    assert five_fields_to_claims({}, batch_doc_id=DOC_ID) == []
    assert (
        five_fields_to_claims(
            {"operational_need": "unknown", "buying_window": 42, "why_now": ["nope"]},
            batch_doc_id=DOC_ID,
        )
        == []
    )


# ------------------------------------------------------------ claims_to_candidates


MODEL = "deepseek/deepseek-v4.1-flash"
FETCHED_AT = "2026-09-28T00:00:00Z"


def _candidates(pairs, **over):
    kwargs = dict(domain="acme.test", fetched_at=FETCHED_AT, model=MODEL)
    kwargs.update(over)
    return claims_to_candidates(pairs, **kwargs)


def test_claims_to_candidates_natural_key_is_a_text_hash():
    cand = _candidates([(Claim(GOOD_1, DOC_ID), 0.9)])[0]
    expected = hashlib.sha256(f"{DOC_ID}:{' '.join(GOOD_1.casefold().split())}".encode("utf-8")).hexdigest()
    assert cand.natural_key == expected
    # Same text, different case/whitespace form => the SAME key (idempotent
    # upsert on re-extraction; the LLM's wording normalization is not a new claim).
    cand2 = _candidates(
        [(Claim("  acme  IS evaluating SIEM vendors AFTER A BREACH last quarter", DOC_ID), 0.9)]
    )[0]
    assert cand2.natural_key == cand.natural_key


def test_claims_to_candidates_order_independent_keys():
    pairs = [(Claim(GOOD_1, DOC_ID), 0.9), (Claim(GOOD_2, DOC_ID), 0.7)]
    keys_forward = {c.summary: c.natural_key for c in _candidates(pairs)}
    keys_reverse = {c.summary: c.natural_key for c in _candidates(list(reversed(pairs)))}
    assert keys_forward == keys_reverse


def test_claims_to_candidates_confidence_noul_evidence_and_observed_at():
    cand = _candidates([(Claim(GOOD_1, DOC_ID), 0.9)])[0]
    assert cand.confidence == 0.9
    assert cand.signal_type == "llm_need"
    assert cand.observed_at == FETCHED_AT  # the cited document's captured date
    assert cand.evidence_data == {
        "doc_id": DOC_ID,
        "model": MODEL,
        "llm_authored": True,
        "gate": "citation_soundness",
        "noul": 0.9,
    }
    assert cand.summary == GOOD_1


def test_claims_to_candidates_confidence_default_when_noul_missing():
    cand = _candidates([(Claim(GOOD_1, DOC_ID), None)])[0]
    assert cand.confidence == 0.5  # documented default; normalize clamps 0..1 anyway
    assert cand.evidence_data["noul"] is None


def test_claims_to_candidates_title_truncated_summary_full():
    long_text = " ".join(["monitoring"] * 40)  # 200+ chars
    cand = _candidates([(Claim(long_text, DOC_ID), 0.8)])[0]
    assert cand.summary == long_text
    assert cand.title is not None and len(cand.title) <= 80
    assert cand.title.endswith("…")


def test_claims_to_candidates_signal_type_override():
    cand = _candidates([(Claim(GOOD_1, DOC_ID), 0.9)], signal_type="need_statement")[0]
    assert cand.signal_type == "need_statement"


# --------------------------------------------- END-TO-END through normalize_batch


def test_llm_need_registered_and_normalize_batch_end_to_end():
    tax = Taxonomy.load("config/signals.yaml")
    spec = tax.get("llm_need")  # registered — raises UnknownSignalType otherwise
    assert spec.category == "operational"
    assert spec.origin == "internal"
    assert spec.catalyst == "primary"
    assert spec.play == "growth_pitch"
    assert spec.label == "LLM-extracted need (gated)"

    cands = _candidates([(Claim(GOOD_1, DOC_ID), 0.9)])
    valid, rejected = normalize_batch(
        cands,
        account=Account(domain="acme.test"),
        source="llm_implement",
        taxonomy=tax,
        now="2026-09-29T00:00:00Z",
    )
    assert rejected == []
    assert len(valid) == 1
    sig = valid[0]
    assert sig.signal_type == "llm_need"
    assert sig.category == "operational"
    assert sig.source == "llm_implement"
    assert sig.confidence == 0.9
    assert sig.observed_at == "2026-09-28"
    assert sig.evidence_data["llm_authored"] is True
    assert sig.evidence_data["gate"] == "citation_soundness"

    # An unregistered type is rejected by the same batch (the registration is
    # what makes the llm_need path work at all).
    bad = SignalCandidate(signal_type="not_a_type", observed_at=FETCHED_AT, natural_key="k")
    valid2, rejected2 = normalize_batch(
        [bad],
        account=Account(domain="acme.test"),
        source="llm_implement",
        taxonomy=tax,
        now="2026-09-29T00:00:00Z",
    )
    assert valid2 == []
    assert [reason for _, reason in rejected2] == ["unknown_signal_type"]


# --------------------------------------------------------------- implement_batch


def test_implement_batch_is_serial_in_input_order():
    active = {"inside": False}
    order: list[str] = []
    seen_threads: set[str] = set()

    def per_doc(spec):
        assert not active["inside"], "per_doc reentered — implement_batch must be serial"
        active["inside"] = True
        seen_threads.add(threading.current_thread().name)
        order.append(f"enter:{spec}")
        order.append(f"exit:{spec}")
        active["inside"] = False
        return f"done:{spec}"

    specs = ["doc-a", "doc-b", "doc-c"]
    assert implement_batch(specs, per_doc=per_doc) == [f"done:{s}" for s in specs]
    assert order == [
        "enter:doc-a", "exit:doc-a", "enter:doc-b", "exit:doc-b", "enter:doc-c", "exit:doc-c",
    ]
    # One thread only: the calling thread (no per-document parallelism in v1).
    assert seen_threads == {threading.current_thread().name}
    assert implement_batch([], per_doc=per_doc) == []
