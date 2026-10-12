"""Task 7 (decide-lab): the multi-family proposer mixer.

Spec: docs/plans/2026-10-11-autoresearch-decide-lab.md — per round, each
proposer family in config/decide.yaml (the planner model plus the two
implementer models) gets the same brief and proposes candidate gate
questions; the batches merge + dedupe before screening (Task 6).

Fully OFFLINE: every family call goes through the documented test seam —
``src/lab/proposers.py`` imports ``src.llm.implement`` as a MODULE and
calls ``implement.call_implementer(...)``, so monkeypatching
``src.llm.implement.call_implementer`` intercepts every family call. No
network, no tokens spent (the conftest _no_network autouse guard backs
this up).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from src.lab.proposers import (
    CANDIDATES_PER_FAMILY,
    DEFAULT_CALL_COST_USD,
    build_brief_messages,
    merge_candidates,
    parse_candidates,
    propose_batch,
    proposer_families,
)

REPO = Path(__file__).resolve().parents[1]

OR_BASE = "https://openrouter.ai/api/v1"


def make_cfg(
    *,
    planner_model="test/planner-m",
    bulk_model="test/bulk-m",
    reasoning_model="test/reasoning-m",
    with_planner=True,
    with_bulk=True,
    with_reasoning=True,
) -> dict:
    """A decide_cfg shaped like config/decide.yaml (ids chosen NOT to match
    any shipped constant — the module must read them from the dict)."""
    cfg: dict = {"implementers": {}}
    if with_planner:
        cfg["planner"] = {"model": planner_model, "base_url": OR_BASE}
    if with_bulk:
        cfg["implementers"]["bulk"] = {"model": bulk_model, "base_url": OR_BASE}
    if with_reasoning:
        cfg["implementers"]["reasoning"] = {
            "model": reasoning_model,
            "base_url": OR_BASE,
            "reasoning_effort": "max",
        }
    return cfg


BRIEF = {
    "target": "claim_survival: 1 when a claim's final G4 outcome is kept, else 0",
    "questions": [
        {
            "text": "Does the document state an operational pain?",
            "error_profile": {"fp": 0.31, "fn": 0.12},
        },
        "Does the document name a competitor?",
    ],
}


class FakeImplementer:
    """A call_implementer double: routes per model id, records every call.

    ``replies`` maps model -> parsed-dict-or-None (None = the never-raise
    failure seam's return for a failed call). ``crash=True`` raises instead —
    even that must not escape propose_batch.
    """

    def __init__(self, replies: dict[str, dict | None] | None = None, *, crash: bool = False):
        self.replies = replies or {}
        self.crash = crash
        self.calls: list[dict] = []

    def __call__(self, messages, *, model, base_url, api_key, **kwargs):
        self.calls.append(
            {
                "messages": messages,
                "model": model,
                "base_url": base_url,
                "api_key": api_key,
                "reasoning_effort": kwargs.get("reasoning_effort"),
            }
        )
        if self.crash:
            raise RuntimeError("double exploded")
        return self.replies.get(model)


def _reply(*questions: str | dict) -> dict:
    return {"questions": [q if isinstance(q, dict) else {"text": q, "criteria": "c"} for q in questions]}


# ------------------------------------------------------------ proposer_families


def test_proposer_families_read_model_ids_from_config():
    cfg = make_cfg()
    fams = proposer_families(cfg)
    assert [f["role"] for f in fams] == ["planner", "bulk", "reasoning"]
    assert [f["model"] for f in fams] == ["test/planner-m", "test/bulk-m", "test/reasoning-m"]
    assert all(f["base_url"] == OR_BASE for f in fams)
    # Only the reasoning family carries an effort (the config's reasoning_effort).
    assert [f["reasoning_effort"] for f in fams] == [None, None, "max"]


def test_proposer_families_skip_absent_or_modelless_blocks():
    # Planner absent, bulk has no model id -> only reasoning survives.
    cfg = make_cfg(with_planner=False)
    cfg["implementers"]["bulk"] = {"base_url": OR_BASE}
    fams = proposer_families(cfg)
    assert [f["role"] for f in fams] == ["reasoning"]
    # Garbage configs never raise: non-dict cfg, no implementers block.
    assert proposer_families(None) == []
    assert proposer_families({"planner": {"model": "x/y"}}) == [
        {"role": "planner", "model": "x/y", "base_url": OR_BASE, "reasoning_effort": None}
    ]


def test_proposer_families_match_shipped_config():
    path = REPO / "config" / "decide.yaml"
    if not path.exists():
        pytest.skip("config/decide.yaml not present")
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    fams = proposer_families(cfg)
    # The three families ARE the three OpenRouter models shipped in the
    # config (planner + the two implementers) — read, never hardcoded.
    assert {f["role"]: f["model"] for f in fams} == {
        "planner": "xiaomi/mimo-v2.6-pro",
        "bulk": "deepseek/deepseek-v4.1-flash",
        "reasoning": "z-ai/glm-5.3-flash",
    }


# --------------------------------------------------------- build_brief_messages


def test_build_brief_messages_contract():
    messages = build_brief_messages(BRIEF)
    assert [m["role"] for m in messages] == ["system", "user"]
    system = messages[0]["content"]
    # STRICT JSON output shape named, with question + criteria keys.
    assert '"questions"' in system
    assert '"text"' in system
    assert '"criteria"' in system
    assert "STRICT JSON" in system
    assert "No prose" in system
    user = messages[1]["content"]
    assert "claim_survival" in user  # the target definition
    assert "Does the document state an operational pain?" in user
    assert "Does the document name a competitor?" in user
    assert "0.31" in user  # the error profile rides the current questions
    assert f"Propose exactly {CANDIDATES_PER_FAMILY}" in user


def test_build_brief_messages_tolerates_sparse_and_garbage_briefs():
    messages = build_brief_messages({})
    assert [m["role"] for m in messages] == ["system", "user"]
    assert "none yet" in messages[1]["content"]
    # Never raises on non-dict briefs.
    for junk in (None, "a string", 42, {"questions": "not-a-list", "target": {"k": 1}}):
        out = build_brief_messages(junk)  # type: ignore[arg-type]
        assert [m["role"] for m in out] == ["system", "user"]
    assert "not-a-list" in build_brief_messages({"questions": "not-a-list"})[1]["content"]


# ------------------------------------------------------------ parse_candidates


def test_parse_candidates_tolerant_shapes():
    raw = {
        "questions": [
            {"text": "Named vendor?", "criteria": "A vendor product is named."},
            "Bare string question",  # LLM shorthand -> criteria ""
            {"criteria": "no text here"},  # dropped: no text
            {},  # dropped
            42,  # dropped: not a dict/str
            {"text": "   ", "criteria": "blank text"},  # dropped: blank
        ]
    }
    out = parse_candidates(raw)
    assert out == [
        {"text": "Named vendor?", "criteria": "A vendor product is named."},
        {"text": "Bare string question", "criteria": ""},
    ]
    # Malformed payloads degrade to empty lists, never raise.
    assert parse_candidates(None) == []
    assert parse_candidates({}) == []
    assert parse_candidates({"questions": "nope"}) == []
    assert parse_candidates({"questions": [None, 3]}) == []


# ------------------------------------------------------------ propose_batch


def test_propose_batch_calls_each_family_once_and_merges(monkeypatch):
    fake = FakeImplementer(
        {
            "test/planner-m": _reply("Q-planner-1", "Q-planner-2"),
            "test/bulk-m": _reply("Q-bulk-1"),
            "test/reasoning-m": _reply("Q-reason-1", "Q-reason-2", "Q-reason-3"),
        }
    )
    monkeypatch.setattr("src.llm.implement.call_implementer", fake)
    out = propose_batch(make_cfg(), BRIEF, api_key="k-test")
    assert [c["text"] for c in out] == [
        "Q-planner-1", "Q-planner-2", "Q-bulk-1",
        "Q-reason-1", "Q-reason-2", "Q-reason-3",
    ]
    # Every candidate carries its family's provenance.
    assert [c["proposer"] for c in out] == [
        "test/planner-m", "test/planner-m", "test/bulk-m",
        "test/reasoning-m", "test/reasoning-m", "test/reasoning-m",
    ]
    assert [c["proposer_role"] for c in out].count("reasoning") == 3
    # One call per family, in config order, same brief for all three.
    assert [c["model"] for c in fake.calls] == [
        "test/planner-m", "test/bulk-m", "test/reasoning-m"
    ]
    assert all(c["base_url"] == OR_BASE for c in fake.calls)
    assert all(c["api_key"] == "k-test" for c in fake.calls)
    assert all(c["messages"] == fake.calls[0]["messages"] for c in fake.calls)
    assert fake.calls[0]["reasoning_effort"] is None
    assert fake.calls[2]["reasoning_effort"] == "max"


def test_propose_batch_one_family_failing_is_tolerated(monkeypatch):
    fake = FakeImplementer(
        {
            "test/planner-m": _reply("Q-planner-1"),
            "test/bulk-m": None,  # the family's outage: its share is empty
            "test/reasoning-m": _reply("Q-reason-1"),
        }
    )
    monkeypatch.setattr("src.llm.implement.call_implementer", fake)
    out = propose_batch(make_cfg(), BRIEF, api_key="k-test")
    assert [c["text"] for c in out] == ["Q-planner-1", "Q-reason-1"]
    assert len(fake.calls) == 3  # the failure does not stop the other families


def test_propose_batch_all_families_failed_returns_empty_with_reason(monkeypatch):
    fake = FakeImplementer(
        {"test/planner-m": None, "test/bulk-m": None, "test/reasoning-m": None}
    )
    monkeypatch.setattr("src.llm.implement.call_implementer", fake)
    failure: dict = {}
    out = propose_batch(make_cfg(), BRIEF, api_key="k-test", failure=failure)
    assert out == []
    assert failure["reason"] == "all_families_failed"


def test_propose_batch_budget_exhaustion_stops_further_calls(monkeypatch):
    # One call costs the conservative DEFAULT_CALL_COST_USD estimate; the
    # budget is truncated to whole cents, so $0.015 admits exactly one call.
    assert DEFAULT_CALL_COST_USD == 0.01
    fake = FakeImplementer(
        {
            "test/planner-m": _reply("Q-planner-1"),
            "test/bulk-m": _reply("Q-bulk-1"),
            "test/reasoning-m": _reply("Q-reason-1"),
        }
    )
    monkeypatch.setattr("src.llm.implement.call_implementer", fake)
    out = propose_batch(make_cfg(), BRIEF, api_key="k-test", budget_usd=0.015)
    assert [c["model"] for c in fake.calls] == ["test/planner-m"]  # stopped after 1
    assert [c["text"] for c in out] == ["Q-planner-1"]

    # $0.02 admits exactly two calls; None (no ceiling) admits all three.
    fake2 = FakeImplementer(dict(fake.replies))
    monkeypatch.setattr("src.llm.implement.call_implementer", fake2)
    propose_batch(make_cfg(), BRIEF, api_key="k-test", budget_usd=0.02)
    assert len(fake2.calls) == 2
    fake3 = FakeImplementer(dict(fake.replies))
    monkeypatch.setattr("src.llm.implement.call_implementer", fake3)
    assert len(propose_batch(make_cfg(), BRIEF, api_key="k-test")) == 3
    assert len(fake3.calls) == 3


def test_propose_batch_budget_below_one_call_is_an_honest_empty(monkeypatch):
    fake = FakeImplementer({m: _reply("Q") for m in ("test/planner-m", "test/bulk-m", "test/reasoning-m")})
    monkeypatch.setattr("src.llm.implement.call_implementer", fake)
    failure: dict = {}
    out = propose_batch(make_cfg(), BRIEF, api_key="k-test", budget_usd=0.005, failure=failure)
    assert out == []
    assert fake.calls == []  # never called: the first call would bust the budget
    assert failure["reason"] == "budget_exhausted"


def test_propose_batch_missing_credentials_never_calls(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    fake = FakeImplementer()
    monkeypatch.setattr("src.llm.implement.call_implementer", fake)
    failure: dict = {}
    out = propose_batch(make_cfg(), BRIEF, failure=failure)  # api_key=None, env unset
    assert out == []
    assert fake.calls == []
    assert failure["reason"] == "missing_credentials"


def test_propose_batch_reads_env_key_when_api_key_omitted(monkeypatch):
    fake = FakeImplementer({"test/planner-m": _reply("Q-planner-1")})
    monkeypatch.setattr("src.llm.implement.call_implementer", fake)
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-key")
    cfg = make_cfg(with_bulk=False, with_reasoning=False)
    out = propose_batch(cfg, BRIEF)
    assert [c["text"] for c in out] == ["Q-planner-1"]
    assert fake.calls[0]["api_key"] == "env-key"


def test_propose_batch_never_raises_even_when_the_seam_explodes(monkeypatch):
    fake = FakeImplementer(crash=True)
    monkeypatch.setattr("src.llm.implement.call_implementer", fake)
    failure: dict = {}
    out = propose_batch(make_cfg(), BRIEF, api_key="k-test", failure=failure)
    assert out == []
    assert failure["reason"] == "crashed"


def test_propose_batch_tolerant_parse_of_family_output(monkeypatch):
    raw = {
        "questions": [
            {"text": "Good question?", "criteria": "yes rule"},
            "Bare string",
            {"criteria": "textless"},
            "   ",
        ]
    }
    fake = FakeImplementer(
        {
            "test/planner-m": raw,
            "test/bulk-m": {"no_questions_key": 1},
            "test/reasoning-m": None,
        }
    )
    monkeypatch.setattr("src.llm.implement.call_implementer", fake)
    out = propose_batch(make_cfg(), BRIEF, api_key="k-test")
    assert [c["text"] for c in out] == ["Good question?", "Bare string"]
    assert out[0]["criteria"] == "yes rule"
    assert out[1]["criteria"] == ""
    assert out[0]["proposer"] == "test/planner-m"


def test_propose_batch_no_families_configured(monkeypatch):
    fake = FakeImplementer()
    monkeypatch.setattr("src.llm.implement.call_implementer", fake)
    failure: dict = {}
    out = propose_batch({}, BRIEF, api_key="k-test", failure=failure)
    assert out == []
    assert fake.calls == []
    assert failure["reason"] == "no_families_configured"


# ----------------------------------------------------------- merge_candidates


def test_merge_candidates_dedupes_by_text_and_criteria_hash():
    b1 = [
        {"text": "Does the doc name a vendor?", "criteria": "A vendor product is named.", "proposer": "A"},
        {"text": "Kept unique 1", "criteria": "c1", "proposer": "A"},
    ]
    b2 = [
        # Same question + same criteria, different case/whitespace -> duplicate.
        {"text": "  does THE DOC name a vendor?  ", "criteria": "a vendor PRODUCT is named.", "proposer": "B"},
        # Same text but DIFFERENT criteria -> a distinct candidate.
        {"text": "Does the doc name a vendor?", "criteria": "A specific product SKU is named.", "proposer": "B"},
    ]
    b3 = [{"text": "Kept unique 3", "criteria": "c3", "proposer": "C"}]
    merged = merge_candidates([b1, b2, b3])
    assert [c["text"] for c in merged] == [
        "Does the doc name a vendor?",
        "Kept unique 1",
        "Does the doc name a vendor?",  # same text, different criteria: kept
        "Kept unique 3",
    ]
    # Stable order + first occurrence wins the provenance on exact duplicates.
    assert [c["proposer"] for c in merged] == ["A", "A", "B", "C"]
    assert merged[2]["criteria"] == "A specific product SKU is named."


def test_merge_candidates_tolerates_garbage():
    assert merge_candidates([]) == []
    assert merge_candidates([[], []]) == []
    # Non-dict entries and blank texts are dropped, never raise.
    junk = [[
        "bare string",
        None,
        42,
        {"text": "", "criteria": "x"},
        {"text": "   ", "criteria": "x"},
        {"criteria": "no text"},
        {"text": "Valid", "criteria": "v"},
    ]]
    assert [c["text"] for c in merge_candidates(junk)] == ["Valid"]


def test_merged_batches_round_trip_through_propose_batch(monkeypatch):
    # The end-to-end shape the round runner consumes: per-family batches from
    # propose_batch, one merged deduped list for Task 6's prescreener.
    fake = FakeImplementer(
        {
            "test/planner-m": _reply("Shared question?", "Planner only"),
            "test/bulk-m": _reply("SHARED  question? ", "Bulk only"),
            "test/reasoning-m": _reply("Reasoning only"),
        }
    )
    monkeypatch.setattr("src.llm.implement.call_implementer", fake)
    cfg = make_cfg()
    batch_a = propose_batch(cfg, BRIEF, api_key="k")
    batch_b = propose_batch(cfg, BRIEF, api_key="k")
    merged = merge_candidates([batch_a, batch_b])
    texts = [c["text"] for c in merged]
    assert texts == ["Shared question?", "Planner only", "Bulk only", "Reasoning only"]
    assert all(json.dumps(c, sort_keys=True) for c in merged)  # JSON-clean dicts
