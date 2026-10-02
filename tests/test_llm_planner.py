"""Tests for the MiMo planner sub-stage (src/llm/).

Fully offline: every HTTP interaction goes through respx, the plan cache
lives in tmp_path, and credentials/consent are monkeypatched explicitly —
no test depends on the developer's real .env, and no test spends a token.
"""

from __future__ import annotations

import hashlib
import json

import httpx
import respx

import src.decide.jev as jev
from src.llm.planner import (
    API_KEY_ENV,
    build_prompt,
    call_planner,
    cache_plan,
    generate_plan,
    load_cached_plan,
    validate_plan,
)

BASE_URL = "https://openrouter.ai/api/v1"
CHAT_URL = f"{BASE_URL}/chat/completions"

# Real knob caps from config/sources.yaml (company_feed: article_follow_max: 5,
# ats_workday: detail_follow_max: 40); fanout ids from src/cli.py --include-fanout
# (sec_formd, federal_register, warn_notices).
ALLOWED_SOURCES = {"ats_workday", "news_rss", "company_feed", "federal_register"}
FANOUT_KEYS = {"federal_register", "sec_formd", "warn_notices"}
KNOB_CAPS = {
    "ats_workday": {"detail_follow_max": 40},
    "company_feed": {"article_follow_max": 5},
}
ICP_EXCERPT = "ICP: 10-200 employee B2B SaaS in North America, seed to Series B."

DECIDE_CFG = {
    "planner": {
        "model": "xiaomi/mimo-v2.6-pro",
        "base_url": BASE_URL,
        "max_plan_steps": 12,
        "plan_cache": True,
    }
}


def _valid_raw() -> dict:
    """A narrowing plan the validator must accept (subset of allowed, knobs lowered)."""
    return {
        "steps": [
            {
                "source_ids": ["ats_workday", "news_rss"],
                "budget_knobs": {"ats_workday": {"detail_follow_max": 20}},
                "acceptance_criteria": ["every step cites a doc_id"],
            },
            {
                "source_ids": ["company_feed"],
                "budget_knobs": {"company_feed": {"article_follow_max": 3}},
                "acceptance_criteria": [],
            },
        ]
    }


def _validate(raw, **over):
    kwargs = dict(
        allowed_sources=set(ALLOWED_SOURCES),
        knob_caps=dict(KNOB_CAPS),
        max_steps=12,
        include_fanout=False,
        fanout_keys=set(FANOUT_KEYS),
    )
    kwargs.update(over)
    return validate_plan(raw, **kwargs)


def _llm_ok_body() -> dict:
    return {"choices": [{"message": {"content": json.dumps(_valid_raw())}}]}


def _kwargs() -> dict:
    return {
        "domain": "acme.test",
        "profile": "default",
        "snapshot_hash": "snap-1",
        "icp_excerpt": ICP_EXCERPT,
        "allowed_sources": set(ALLOWED_SOURCES),
        "knob_caps": dict(KNOB_CAPS),
        "fanout_keys": set(FANOUT_KEYS),
        "include_fanout": False,
        "dry_run": False,
    }


def _grant_creds(monkeypatch) -> None:
    monkeypatch.setenv(API_KEY_ENV, "or-test-dummy")
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", jev.DATA_EXIT_CONSENT)


def _revoke_creds(monkeypatch) -> None:
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    monkeypatch.delenv("SIGNALS_DECIDE_DATA_EXIT", raising=False)


# ---------------------------------------------------------------- validate_plan


def test_validate_plan_accepts_narrowing_plan():
    plan, why = _validate(_valid_raw())
    assert why == "ok"
    assert [step.source_ids for step in plan.steps] == [
        ["ats_workday", "news_rss"],
        ["company_feed"],
    ]
    # knobs are lowered, never raised (20 < 40 shipped, 3 < 5 shipped)
    assert plan.steps[0].budget_knobs == {"ats_workday": {"detail_follow_max": 20}}
    assert plan.steps[1].budget_knobs == {"company_feed": {"article_follow_max": 3}}
    assert plan.steps[0].acceptance_criteria == ["every step cites a doc_id"]
    assert plan.steps[1].acceptance_criteria == []


def test_validate_plan_preserves_step_order():
    raw = _valid_raw()
    raw["steps"] = list(reversed(raw["steps"]))
    plan, why = _validate(raw)
    assert why == "ok"
    assert [step.source_ids for step in plan.steps] == [
        ["company_feed"],
        ["ats_workday", "news_rss"],
    ]


def test_validate_plan_rejects_unknown_source():
    raw = _valid_raw()
    raw["steps"][0]["source_ids"] = ["made_up_source"]
    plan, why = _validate(raw)
    assert (plan, why) == (None, "unknown_source:made_up_source")


def test_validate_plan_rejects_budget_raise():
    raw = _valid_raw()
    raw["steps"][0]["budget_knobs"] = {"ats_workday": {"detail_follow_max": 41}}
    plan, why = _validate(raw)
    assert (plan, why) == (None, "budget_raise:ats_workday.detail_follow_max")


def test_validate_plan_accepts_knob_at_exact_cap():
    raw = _valid_raw()
    raw["steps"][0]["budget_knobs"] = {"ats_workday": {"detail_follow_max": 40}}
    plan, why = _validate(raw)
    assert why == "ok"
    assert plan.steps[0].budget_knobs == {"ats_workday": {"detail_follow_max": 40}}


def test_validate_plan_rejects_unknown_knob_name():
    raw = _valid_raw()
    raw["steps"][0]["budget_knobs"] = {"ats_workday": {"invented_knob": 1}}
    plan, why = _validate(raw)
    assert plan is None
    assert why == "unknown_knob:ats_workday.invented_knob"


def test_validate_plan_rejects_knob_for_source_outside_step():
    raw = _valid_raw()
    # company_feed knob attached to a step that does not run company_feed
    raw["steps"][0]["budget_knobs"] = {"company_feed": {"article_follow_max": 2}}
    plan, why = _validate(raw)
    assert plan is None
    assert why.startswith("unknown_knob:company_feed")


def test_validate_plan_fanout_widening_rejected_then_allowed():
    raw = {
        "steps": [
            {"source_ids": ["federal_register"], "budget_knobs": {}, "acceptance_criteria": []},
        ]
    }
    # the run did NOT pass --include-fanout: the planner may not add it back
    assert _validate(raw) == (None, "fanout_not_allowed:federal_register")
    # the same plan is accepted when the run opted into fanout
    plan, why = _validate(raw, include_fanout=True)
    assert why == "ok"
    assert plan.steps[0].source_ids == ["federal_register"]


def test_validate_plan_rejects_too_many_steps():
    steps = _valid_raw()["steps"]
    raw = {"steps": steps * 13}
    assert _validate(raw) == (None, "too_many_steps")


def test_validate_plan_rejects_negative_knob():
    raw = _valid_raw()
    raw["steps"][0]["budget_knobs"] = {"ats_workday": {"detail_follow_max": -1}}
    assert _validate(raw) == (None, "malformed")


def test_validate_plan_rejects_malformed():
    for raw in (None, "nope", 42, {}, {"steps": "two"}, {"steps": [{"source_ids": []}]}):
        assert _validate(raw) == (None, "malformed")


# ----------------------------------------------------------------- call_planner


@respx.mock
def test_call_planner_posts_bearer_and_parses_json():
    route = respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=_llm_ok_body()))
    raw = call_planner("plan this run", model="xiaomi/mimo-v2.6-pro", base_url=BASE_URL, api_key="or-dummy")
    assert raw == _valid_raw()
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Bearer or-dummy"
    body = json.loads(req.content)
    assert body["model"] == "xiaomi/mimo-v2.6-pro"
    assert body["temperature"] == 0
    assert body["messages"] == [{"role": "user", "content": "plan this run"}]


@respx.mock
def test_call_planner_strips_json_fence():
    fenced = "```json\n" + json.dumps(_valid_raw()) + "\n```"
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": fenced}}]})
    )
    assert call_planner("p", model="m", base_url=BASE_URL, api_key="k") == _valid_raw()


@respx.mock
def test_call_planner_malformed_content_returns_none():
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": "I cannot emit JSON today"}}]}
        )
    )
    assert call_planner("p", model="m", base_url=BASE_URL, api_key="k") is None


@respx.mock
def test_call_planner_http_500_returns_none():
    respx.post(CHAT_URL).mock(return_value=httpx.Response(500, json={"error": "boom"}))
    assert call_planner("p", model="m", base_url=BASE_URL, api_key="k") is None


@respx.mock
def test_call_planner_transport_error_returns_none():
    respx.post(CHAT_URL).mock(side_effect=httpx.ConnectError("connection refused"))
    assert call_planner("p", model="m", base_url=BASE_URL, api_key="k") is None


# ------------------------------------------------------------------- plan cache


def test_plan_cache_round_trip(tmp_path):
    plan, _ = _validate(_valid_raw())
    cache_plan(tmp_path, "acme.test", "default", "snap-1", plan)
    assert load_cached_plan(tmp_path, "acme.test", "default", "snap-1") == plan


def test_plan_cache_filename_is_sha256_of_triple(tmp_path):
    plan, _ = _validate(_valid_raw())
    cache_plan(tmp_path, "acme.test", "default", "snap-1", plan)
    key = hashlib.sha256(b"acme.test|default|snap-1").hexdigest()
    assert (tmp_path / f"{key}.json").exists()


def test_load_cached_plan_missing_or_corrupt_returns_none(tmp_path):
    assert load_cached_plan(tmp_path, "acme.test", "default", "snap-1") is None
    key = hashlib.sha256(b"acme.test|default|snap-1").hexdigest()
    (tmp_path / f"{key}.json").write_text("{not json", encoding="utf-8")
    assert load_cached_plan(tmp_path, "acme.test", "default", "snap-1") is None
    # valid JSON but wrong shape also degrades to a cache miss, never an error
    (tmp_path / f"{key}.json").write_text('{"nope": 1}', encoding="utf-8")
    assert load_cached_plan(tmp_path, "acme.test", "default", "snap-1") is None


# ---------------------------------------------------------------- generate_plan


@respx.mock
def test_generate_plan_generates_then_cache_hit_makes_zero_http_calls(monkeypatch, tmp_path):
    route = respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=_llm_ok_body()))
    _grant_creds(monkeypatch)
    cache = tmp_path / "plan_cache"

    plan, why = generate_plan(decide_cfg=DECIDE_CFG, cache_dir=cache, **_kwargs())
    assert why == "generated"
    assert plan is not None
    assert [step.source_ids for step in plan.steps] == [
        ["ats_workday", "news_rss"],
        ["company_feed"],
    ]
    assert route.call_count == 1

    plan2, why2 = generate_plan(decide_cfg=DECIDE_CFG, cache_dir=cache, **_kwargs())
    assert why2 == "cache"
    assert plan2 == plan
    assert route.call_count == 1  # zero new HTTP calls


@respx.mock
def test_generate_plan_stale_cache_not_served_when_scope_shrinks(monkeypatch, tmp_path):
    """Q: a cache hit is re-validated against the CURRENT inputs — a scope
    change after caching must never serve the stale plan (here: no
    credentials either, so a cache miss degrades to missing_credentials)."""
    route = respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=_llm_ok_body()))
    _grant_creds(monkeypatch)
    cache = tmp_path / "plan_cache"

    plan, why = generate_plan(decide_cfg=DECIDE_CFG, cache_dir=cache, **_kwargs())
    assert why == "generated"
    assert route.call_count == 1

    # Config change: company_feed is no longer allowed for this account.
    _revoke_creds(monkeypatch)
    kwargs = _kwargs()
    kwargs["allowed_sources"] = {"ats_workday", "news_rss"}
    plan2, why2 = generate_plan(decide_cfg=DECIDE_CFG, cache_dir=cache, **kwargs)
    assert (plan2, why2) == (None, "missing_credentials")  # stale plan NOT served
    assert route.call_count == 1  # still no HTTP (credentials missing)
    # the stale file was dropped so a later credentialed run regenerates
    assert load_cached_plan(cache, "acme.test", "default", "snap-1") is None


@respx.mock
def test_generate_plan_stale_cache_regenerated_against_current_scope(monkeypatch, tmp_path):
    """Q: with credentials, a stale (out-of-scope) cache is dropped and the
    regenerated plan validates against the current allowed sources."""
    _grant_creds(monkeypatch)
    cache = tmp_path / "plan_cache"
    route = respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=_llm_ok_body()))
    plan, why = generate_plan(decide_cfg=DECIDE_CFG, cache_dir=cache, **_kwargs())
    assert why == "generated"
    assert route.call_count == 1

    narrowed_raw = {
        "steps": [
            {
                "source_ids": ["ats_workday", "news_rss"],
                "budget_knobs": {"ats_workday": {"detail_follow_max": 20}},
                "acceptance_criteria": [],
            }
        ]
    }
    route.mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(narrowed_raw)}}]}
        )
    )
    kwargs = _kwargs()
    kwargs["allowed_sources"] = {"ats_workday", "news_rss"}
    plan2, why2 = generate_plan(decide_cfg=DECIDE_CFG, cache_dir=cache, **kwargs)
    assert why2 == "generated"  # regenerated — never the stale plan
    assert [step.source_ids for step in plan2.steps] == [["ats_workday", "news_rss"]]
    assert route.call_count == 2

    # the refreshed cache now validates for the narrowed scope
    plan3, why3 = generate_plan(decide_cfg=DECIDE_CFG, cache_dir=cache, **kwargs)
    assert why3 == "cache"
    assert plan3 == plan2
    assert route.call_count == 2  # zero new HTTP calls


@respx.mock
def test_generate_plan_dry_run_makes_no_http_call_and_needs_no_keys(monkeypatch, tmp_path):
    _revoke_creds(monkeypatch)
    route = respx.post(CHAT_URL).mock(side_effect=AssertionError("dry-run must not call the LLM"))
    kwargs = _kwargs()
    kwargs["dry_run"] = True
    plan, why = generate_plan(decide_cfg=DECIDE_CFG, cache_dir=tmp_path, **kwargs)
    assert (plan, why) == (None, "dry_run")
    assert route.call_count == 0


@respx.mock
def test_generate_plan_missing_api_key_returns_missing_credentials(monkeypatch, tmp_path):
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", jev.DATA_EXIT_CONSENT)
    route = respx.post(CHAT_URL).mock(side_effect=AssertionError("no key means no HTTP"))
    plan, why = generate_plan(decide_cfg=DECIDE_CFG, cache_dir=tmp_path, **_kwargs())
    assert (plan, why) == (None, "missing_credentials")
    assert route.call_count == 0


@respx.mock
def test_generate_plan_wrong_consent_returns_missing_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv(API_KEY_ENV, "or-test-dummy")
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", "yes I understand")
    route = respx.post(CHAT_URL).mock(side_effect=AssertionError("no consent means no HTTP"))
    plan, why = generate_plan(decide_cfg=DECIDE_CFG, cache_dir=tmp_path, **_kwargs())
    assert (plan, why) == (None, "missing_credentials")
    assert route.call_count == 0


@respx.mock
def test_generate_plan_llm_error(monkeypatch, tmp_path):
    _grant_creds(monkeypatch)
    respx.post(CHAT_URL).mock(return_value=httpx.Response(500, json={"error": "upstream down"}))
    plan, why = generate_plan(decide_cfg=DECIDE_CFG, cache_dir=tmp_path, **_kwargs())
    assert (plan, why) == (None, "llm_error")


@respx.mock
def test_generate_plan_invalid_llm_plan_rejected_and_not_cached(monkeypatch, tmp_path):
    raw = _valid_raw()
    raw["steps"][0]["source_ids"] = ["made_up_source"]
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(raw)}}]})
    )
    _grant_creds(monkeypatch)
    cache = tmp_path / "plan_cache"
    plan, why = generate_plan(decide_cfg=DECIDE_CFG, cache_dir=cache, **_kwargs())
    assert (plan, why) == (None, "unknown_source:made_up_source")
    # a rejected plan is never cached — the deterministic plan stays the fallback
    assert load_cached_plan(cache, "acme.test", "default", "snap-1") is None


# ----------------------------------------------------------------- build_prompt


def test_build_prompt_mentions_every_allowed_source():
    prompt = build_prompt(
        ICP_EXCERPT, "acme.test", sorted(ALLOWED_SOURCES), KNOB_CAPS, 12
    )
    for source in ("ats_workday", "company_feed", "news_rss", "federal_register"):
        assert source in prompt
    # knob names and shipped caps are shown so the model can lower them
    assert "detail_follow_max" in prompt
    assert "article_follow_max" in prompt


def test_build_prompt_narrowing_contract_and_icp_excerpt():
    prompt = build_prompt(
        ICP_EXCERPT, "acme.test", sorted(ALLOWED_SOURCES), KNOB_CAPS, 12
    ).lower()
    for phrase in ("never invent", "never add", "never raise"):
        assert phrase in prompt
    assert "icp: 10-200 employee b2b saas in north america, seed to series b." in prompt
    # the strict JSON contract names every required key
    for token in ('"steps"', '"source_ids"', '"budget_knobs"', '"acceptance_criteria"'):
        assert token in prompt
    assert "12" in prompt  # the step cap is stated
