"""Tests for the consistency sampler (src/decide/audit.py).

Offline: scripted MockDecider subclasses only — no network. The single
llm_live test follows the test_live_jev_ping precedent exactly (live_fetch
marker for conftest's network gate, skipif without TYPESAFE_API_KEY).
"""

from __future__ import annotations

import copy
import math
import os
import statistics

import pytest

from src.decide import (
    Decision,
    LiveDecider,
    MockDecider,
    sample_consistency,
)

BASE = "https://api.typesafe.ai/v1/systemone"


class ScriptedDecider(MockDecider):
    """MockDecider subclass cycling scripted answers per call index.

    MockDecider is deterministic per question id, which cannot express a
    wobble; this subclass hands out ``scripts[qid][call_index % len]`` per
    call and can be told to fail whole calls (``fail_on`` set of call
    indexes -> ``ok=False``). Every state it is handed is recorded.
    """

    def __init__(
        self,
        scripts: dict[str, list[dict]],
        fail_on: set[int] | None = None,
    ) -> None:
        super().__init__({})
        self.scripts = scripts
        self.fail_on = fail_on or set()
        self.calls = 0
        self.states: list[str | dict] = []

    def decide(self, state: str | dict, questions: dict[str, dict]) -> Decision:
        idx = self.calls
        self.calls += 1
        self.states.append(state)
        if idx in self.fail_on:
            return Decision(applies=True, ok=False, answers={}, raw_tokens=0)
        answers = {}
        for qid in questions:
            script = self.scripts.get(qid)
            if script is not None:
                answers[qid] = script[idx % len(script)]
        return Decision(applies=True, ok=True, answers=answers, raw_tokens=1)


def _noul_q() -> dict:
    return {"type": "noul", "instructions": "Score on-topic-ness."}


def _choice_q() -> dict:
    return {
        "type": "choice",
        "instructions": "Pick a lane.",
        "criteria": ["deep", "quick"],
    }


def test_std_dev_math_on_scripted_wobble():
    """A scripted noul wobble yields exact std/mean/min/max (float tolerance)."""
    wobble = [{"noul": v} for v in (0.2, 0.4, 0.6, 0.8)]
    decider = ScriptedDecider({"on_topic": wobble})
    result = sample_consistency(decider, {"doc": "payload"}, {"on_topic": _noul_q()}, samples=8)
    expected = [0.2, 0.4, 0.6, 0.8, 0.2, 0.4, 0.6, 0.8]
    entry = result["questions"]["on_topic"]
    assert result["samples"] == 8
    assert result["parse_failures"] == 0
    assert entry["n_valid"] == 8
    assert entry["std"] == pytest.approx(statistics.stdev(expected))
    assert entry["mean"] == pytest.approx(statistics.fmean(expected))
    assert entry["min"] == pytest.approx(0.2)
    assert entry["max"] == pytest.approx(0.8)
    # noul questions carry no label histogram / conflicts keys.
    assert "labels" not in entry
    assert "conflicts" not in entry


def test_uid_fresh_and_non_mutating():
    """Every call sees a unique uid; the caller's state object is untouched."""
    decider = ScriptedDecider({"on_topic": [{"noul": 0.5}]})
    original = {"domain": "acme.com", "notes": ["a", "b"]}
    snapshot = copy.deepcopy(original)

    result = sample_consistency(decider, original, {"on_topic": _noul_q()}, samples=6)
    assert result["samples"] == 6
    assert len(decider.states) == 6

    uids = [s["_audit_uid"] for s in decider.states]
    assert len(set(uids)) == 6  # fresh uid per call
    for uid in uids:
        assert isinstance(uid, str) and len(uid) == 32  # uuid4 hex
        int(uid, 16)  # parses as hex

    # The caller's original state is byte-identical and gains no uid key.
    assert original == snapshot
    assert "_audit_uid" not in original
    assert "uid" not in original  # a legitimate "uid" state field is not clobbered
    assert original["notes"] == ["a", "b"]


def test_choice_labels_and_conflicts():
    """A flipping choice question builds the label histogram; conflicts == 1."""
    flips = [
        {"choice": "deep", "probabilities": {"deep": 0.8, "quick": 0.2}},
        {"choice": "quick", "probabilities": {"deep": 0.3, "quick": 0.7}},
    ]
    decider = ScriptedDecider({"lane": flips, "on_topic": [{"noul": 0.9}]})
    questions = {"lane": _choice_q(), "on_topic": _noul_q()}
    result = sample_consistency(decider, "state", questions, samples=4)

    lane = result["questions"]["lane"]
    assert lane["labels"] == {"deep": 2, "quick": 2}
    assert lane["conflicts"] == 1  # >1 concrete label across samples
    # Values are the CHOSEN label's probabilities: deep, quick, deep, quick.
    assert lane["mean"] == pytest.approx(statistics.fmean([0.8, 0.7, 0.8, 0.7]))
    assert lane["n_valid"] == 4

    # A stable noul sibling has no labels/conflicts and zero conflicts noise.
    on_topic = result["questions"]["on_topic"]
    assert "labels" not in on_topic
    assert "conflicts" not in on_topic


def test_parse_failures_on_error():
    """An ok=False call is a parse failure for every question in that call."""
    decider = ScriptedDecider(
        {
            "on_topic": [{"noul": 0.5}, {"noul": 0.7}],
            "lane": [{"choice": "deep", "probabilities": {"deep": 0.9, "quick": 0.1}}],
        },
        fail_on={2},  # third call (0-indexed) fails whole-call
    )
    questions = {"on_topic": _noul_q(), "lane": _choice_q()}
    result = sample_consistency(decider, "state", questions, samples=5)

    # 2 questions x 1 failed call = 2 parse failures.
    assert result["parse_failures"] == 2
    assert result["samples"] == 5
    for entry in result["questions"].values():
        assert entry["n_valid"] == 4  # metrics from valid samples only
        assert not math.isnan(entry["mean"])


def test_fast_path_samples_3():
    """samples=3 works; std uses the len>=2 rule (statistics.stdev on 3 values)."""
    decider = ScriptedDecider({"on_topic": [{"noul": v} for v in (0.1, 0.5, 0.9)]})
    result = sample_consistency(decider, "state", {"on_topic": _noul_q()}, samples=3)
    entry = result["questions"]["on_topic"]
    assert entry["n_valid"] == 3
    assert entry["std"] == pytest.approx(statistics.stdev([0.1, 0.5, 0.9]))
    assert entry["std"] > 0.0
    assert entry["mean"] == pytest.approx(0.5)


def test_string_state_supported():
    """A str state works: annotated per call, original string unchanged."""
    decider = ScriptedDecider({"on_topic": [{"noul": 0.5}]})
    original = "verbatim state text"
    result = sample_consistency(decider, original, {"on_topic": _noul_q()}, samples=4)
    assert result["samples"] == 4

    assert isinstance(original, str) and original == "verbatim state text"
    assert len(decider.states) == 4
    assert len(set(decider.states)) == 4  # unique per call via the uid line
    for recorded in decider.states:
        assert isinstance(recorded, str)
        assert recorded.startswith(original)


@pytest.mark.llm_live
# live_fetch: conftest's network gate only opens for known markers; this test
# genuinely hits the TypeSafe endpoint when run opt-in (-m llm_live with keys).
@pytest.mark.live_fetch
@pytest.mark.skipif(
    not os.environ.get("TYPESAFE_API_KEY"),
    reason="TYPESAFE_API_KEY not set; live consistency ping skipped",
)
def test_live_consistency_ping():
    """Tiny real 3-question rubric, samples=5; result-shape assertions only."""
    decider = LiveDecider(
        base_url=BASE,
        model="jev-1.13.0",
        api_key=os.environ["TYPESAFE_API_KEY"],
        timeout_s=25.0,
        max_retries=0,
    )
    questions = {
        "ping_topic": {
            "type": "noul",
            "instructions": "Does this state describe a real system ping? Answer with a noul score.",
        },
        "ping_lane": {
            "type": "choice",
            "instructions": "Classify the ping's intent.",
            "criteria": ["smoke", "probe"],
        },
        "ping_focus": {
            "type": "noul",
            "instructions": "Is the state one line long? Answer with a noul score.",
        },
    }
    result = sample_consistency(
        decider,
        "Signals llm_live consistency ping: a one-line state payload for the sampler.",
        questions,
        samples=5,
    )
    # Shape assertions only — the real distributions tune the bands downstream.
    assert result["samples"] == 5
    assert set(result["questions"]) == set(questions)
    assert isinstance(result["parse_failures"], int)
    for qid, entry in result["questions"].items():
        assert {"std", "mean", "min", "max", "n_valid"} <= set(entry), qid
        assert 0 <= entry["n_valid"] <= 5, qid
        if entry["n_valid"]:
            assert 0.0 <= entry["min"] <= entry["max"] <= 1.0, qid
    assert isinstance(result["questions"]["ping_lane"].get("labels"), dict)
    assert isinstance(result["questions"]["ping_lane"].get("conflicts"), int)
