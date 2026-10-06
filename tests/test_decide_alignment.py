"""Tests for entity alignment (src/decide/alignment.py) — the discover-frame
annotation pass (waves-2/3 Task 7).

Semantics pinned here (see the alignment module docstring for the reasoning):
- ONE Jev request per (mention, candidate) pair: a single ``link_state``
  Score question whose THREE ordered levels ARE the outcomes, plus FOUR
  companion Nouls (same_name/same_domain/same_location/same_industry) in
  the SAME request.
- Routing = round to the NEAREST level (``int(score + 0.5)`` clamped) —
  NO thresholds; the half edge pins UP (0.5 -> related, 1.5 -> same).
- The annotation is ENFORCE-ONLY: shadow runs the pairs rows-only and the
  returned alignment is unavailable, so the caller's queue is untouched.
- Never raises past the seam: NullDecider / Jev error / malformed score all
  degrade to {"outcome": "unavailable", "score": None, "fields": {}}.
- align_candidates walks the producer's ranking in list order under the
  ``max_pairs`` cap (budget = requests), skips candidates without a join
  key, and returns ONLY alignments that bind.

Fully offline: MockDecider only, no network, no respx.
"""

from __future__ import annotations

import copy

from src.decide import Decision, DecideLedger, MockDecider, NullDecider
from src.decide.alignment import MAX_ALIGN_PAIRS, align_candidates, align_pair, candidate_key

RUN = "run-align-test"

# The three ordered level wordings (the middle one is the tuning knob).
LEVELS = [
    "different company",
    "closely related, may or may not be the same — a subsidiary, parent, "
    "similarly-named company, or a product line that could plausibly refer to either",
    "one and the same company",
]

FIELDS = ("same_name", "same_domain", "same_location", "same_industry")


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


def _link_decider(
    score=1.4,
    nouls=None,
    ok=True,
    applies=True,
    tokens=77,
    raise_exc=None,
) -> CountingDecider:
    """A decider answering the whole five-question batch in ONE Decision
    (MockDecider keys the verdict on the first question id and the Decision
    carries every answer, the test_decide_gates.py pattern)."""
    answers: dict = {
        "link_state": {
            "score": score,
            "legend": list(LEVELS),
            "probabilities": [0.1, 0.2, 0.7],
            "confidence": 0.9,
        }
    }
    for field, value in (nouls or {}).items():
        answers[field] = {"noul": value}
    verdict = Decision(
        applies=applies,
        ok=ok,
        answers=answers if ok and applies else {},
        raw_tokens=tokens,
    )

    class _Raising(CountingDecider):
        def decide(self, state, questions):
            self.calls.append((copy.deepcopy(state), copy.deepcopy(questions)))
            raise raise_exc

    if raise_exc is not None:
        return _Raising({"link_state": verdict})
    return CountingDecider({"link_state": verdict})


def _mention() -> dict:
    return {
        "name": "Acme Corp",
        "domain": None,
        "location": "Austin, TX",
        "industry": "industrial robots",
        "snippet": "Acme Corp said it would expand its Austin robotics line.",
    }


def _company(domain: str = "acme.com") -> dict:
    return {
        "name": "Acme Corporation",
        "domain": domain,
        "hq": "Austin, Texas",
        "industry": "robotics",
    }


def _ok_pair(decider, mode="enforce", gate_cfg=None, ledger=None):
    ledger = ledger or DecideLedger()
    out = align_pair(
        _mention(), _company(), decider, gate_cfg, ledger, RUN, mode
    )
    return out, ledger


# ── routing: round to the nearest level, no thresholds ──────────────────────


def test_routing_table_rounds_to_nearest_level():
    for score, outcome in [
        (0.25, "different"),
        (0.49, "different"),
        (1.4, "related"),
        (1.9, "same"),
    ]:
        decider = _link_decider(score=score)
        out, _ = _ok_pair(decider)
        assert out["outcome"] == outcome, score
        assert out["score"] == score


def test_rounding_half_edge_pins_up_exactly_per_formula():
    """PIN the exact routing formula min(int(score + 0.5), 2): the half
    edge rounds UP — 0.5 becomes level 1 (related), 1.5 becomes level 2
    (same). No threshold exists at any other point."""
    for score, outcome in [(0.5, "related"), (1.5, "same"), (2.4, "same")]:
        decider = _link_decider(score=score)
        out, _ = _ok_pair(decider)
        assert out["outcome"] == outcome, score


def test_routing_clamps_extremes_without_raising():
    decider = _link_decider(score=-1.7)
    out, _ = _ok_pair(decider)
    assert out["outcome"] == "different"


# ── the request shape: one call, five questions ─────────────────────────────


def test_one_request_per_pair_with_all_five_questions():
    decider = _link_decider(
        score=1.4,
        nouls={"same_name": 0.9, "same_domain": 0.8, "same_location": 0.3,
               "same_industry": 0.6},
    )
    out, ledger = _ok_pair(decider)

    assert decider.n_calls == 1
    state, questions = decider.calls[0]
    # The state carries both sides verbatim (mention + company profiles).
    assert state["mention"]["name"] == "Acme Corp"
    assert state["mention"]["snippet"].startswith("Acme Corp said")
    assert state["company"]["domain"] == "acme.com"
    # ONE Score question with the three ordered levels as its criteria...
    assert questions["link_state"]["type"] == "score"
    assert questions["link_state"]["criteria"] == LEVELS
    # ...plus the four companion Nouls in the SAME request.
    for field in FIELDS:
        assert questions[field]["type"] == "noul"
        assert questions[field]["instructions"]
    # Enforce returns the binding annotation; fields carry the nouls.
    assert out == {
        "outcome": "related",
        "score": 1.4,
        "fields": {
            "same_name": 0.9,
            "same_domain": 0.8,
            "same_location": 0.3,
            "same_industry": 0.6,
        },
    }
    # ONE ledger row per pair, boundary "entity_alignment".
    assert len(ledger.rows) == 1
    row = ledger.rows[0]
    assert row["gate"] == "entity_alignment"
    assert row["boundary"] == "entity_alignment"
    assert row["called"] is True
    assert row["outcome"] == "related"
    assert row["raw_tokens"] == 77


def test_mention_and_company_built_defensively_from_missing_keys():
    decider = _link_decider()
    ledger = DecideLedger()
    align_pair({}, {}, decider, {}, ledger, RUN, "enforce")
    state, _ = decider.calls[0]
    assert state["mention"] == {
        "name": None, "domain": None, "location": None, "industry": None,
        "snippet": "",
    }
    assert state["company"] == {"name": None, "domain": None, "hq": None,
                                "industry": None}


def test_per_field_nouls_absent_surface_as_none_but_stay_in_fields():
    decider = _link_decider(score=0.25, nouls={"same_name": 0.95})
    out, _ = _ok_pair(decider)
    assert out["outcome"] == "different"
    assert out["fields"] == {
        "same_name": 0.95,
        "same_domain": None,
        "same_location": None,
        "same_industry": None,
    }


# ── never raises past the seam: unavailable postures ────────────────────────


def test_null_decider_is_unavailable_with_a_not_called_row():
    ledger = DecideLedger()
    out = align_pair(_mention(), _company(), NullDecider(), {}, ledger, RUN, "enforce")
    assert out == {"outcome": "unavailable", "score": None, "fields": {}}
    row = ledger.rows[0]
    assert row["called"] is False
    assert "outcome" not in row  # not-applicable rows omit the outcome
    assert row["error"] is None


def test_malformed_score_is_unavailable():
    for bad_answers in (
        {"same_name": {"noul": 0.9}},  # link_state missing entirely
        {"link_state": {"legend": LEVELS}},  # score key missing
        {"link_state": {"score": "high"}},  # non-numeric score
        {"link_state": {"score": True}},  # boolean is not a score
    ):
        decider = CountingDecider(
            {"link_state": Decision(applies=True, ok=True, answers=bad_answers,
                                    raw_tokens=10)}
        )
        ledger = DecideLedger()
        out = align_pair(_mention(), _company(), decider, {}, ledger, RUN, "enforce")
        assert out == {"outcome": "unavailable", "score": None, "fields": {}}, bad_answers
        row = ledger.rows[0]
        assert row["error"] == "invalid_score"
        assert row["outcome"] == "unavailable"


def test_jev_error_is_unavailable_and_row_records_errored():
    ledger = DecideLedger()
    out = _ok_pair(_link_decider(ok=False), ledger=ledger)[0]
    assert out == {"outcome": "unavailable", "score": None, "fields": {}}
    row = ledger.rows[0]
    assert row["error"] == "jev_error"
    assert row["outcome"] == "errored"
    assert row["called"] is True


def test_decider_raising_degrades_to_unavailable():
    ledger = DecideLedger()
    out = _ok_pair(_link_decider(raise_exc=RuntimeError("boom")), ledger=ledger)[0]
    assert out == {"outcome": "unavailable", "score": None, "fields": {}}
    assert ledger.rows[0]["outcome"] == "errored"


# ── mode postures: enforce binds, shadow is rows-only ───────────────────────


def test_shadow_records_the_would_be_row_but_returns_unavailable():
    decider = _link_decider(score=1.4, nouls={"same_name": 0.9, "same_domain": 0.2,
                                              "same_location": 0.4,
                                              "same_industry": 0.1})
    ledger = DecideLedger()
    out = _ok_pair(decider, mode="shadow", ledger=ledger)[0]
    # The queue annotation never binds in shadow...
    assert out == {"outcome": "unavailable", "score": None, "fields": {}}
    # ...but the row records exactly what enforce WOULD have annotated.
    row = ledger.rows[0]
    assert row["called"] is True
    assert row["outcome"] == "related"
    assert row["answers"]["link_state"]["score"] == 1.4


def test_non_enforce_mode_fails_safe_inert():
    decider = _link_decider()
    out, _ = _ok_pair(decider, mode="off")
    assert out["outcome"] == "unavailable"
    assert decider.n_calls == 1  # the pair still ran rows-only


# ── align_candidates: ranking-order budget + join keys ──────────────────────


def _candidates(n: int) -> list[dict]:
    return [
        {"domain": f"cand{i}.com", "label": f"Candidate {i}", "score": n - i}
        for i in range(n)
    ]


def test_align_candidates_walks_list_order_under_the_cap():
    decider = _link_decider(nouls={"same_name": 0.9})
    ledger = DecideLedger()
    out = align_candidates(
        {"name": "Acme"}, _candidates(5), decider, {"max_pairs": 3}, ledger,
        RUN, "enforce",
    )
    # Exactly the TOP THREE of the producer's ranking got the budget...
    assert decider.n_calls == 3
    assert set(out) == {"cand0.com", "cand1.com", "cand2.com"}
    for key, alignment in out.items():
        assert alignment["outcome"] == "related"
        assert alignment["fields"]["same_name"] == 0.9
    assert len(ledger.rows) == 3


def test_align_candidates_default_cap_is_the_module_constant():
    assert MAX_ALIGN_PAIRS == 10
    decider = _link_decider()
    out = align_candidates({"name": "Acme"}, _candidates(4), decider, {}, DecideLedger(),
                           RUN, "enforce")
    assert decider.n_calls == 4
    assert set(out) == {"cand0.com", "cand1.com", "cand2.com", "cand3.com"}


def test_align_candidates_bad_max_pairs_falls_back_to_default():
    decider = _link_decider()
    out = align_candidates({"name": "Acme"}, _candidates(2), decider,
                           {"max_pairs": "lots"}, DecideLedger(), RUN, "enforce")
    assert decider.n_calls == 2


def test_align_candidates_skips_candidates_without_a_join_key():
    candidates = [
        {"domain": "has.com", "label": "Has"},
        {"score": 1, "stage": "wikidata"},  # no domain/url/name: skipped, no spend
        {"domain": "also.com", "label": "Also"},
    ]
    decider = _link_decider()
    out = align_candidates({"name": "Acme"}, candidates, decider, {}, DecideLedger(),
                           RUN, "enforce")
    assert decider.n_calls == 2
    assert set(out) == {"has.com", "also.com"}


class _OrderSensitiveDecider(MockDecider):
    """Scores 1.4 on the FIRST call and 0.2 afterwards: makes first-wins vs
    last-wins observable when two candidates share a join key."""

    def __init__(self) -> None:
        super().__init__({})
        self.n_calls = 0

    def decide(self, state, questions):
        self.n_calls += 1
        score = 1.4 if self.n_calls == 1 else 0.2
        answers = {
            "link_state": {"score": score, "legend": list(LEVELS), "confidence": 0.9},
            "same_name": {"noul": 0.9},
        }
        return Decision(applies=True, ok=True, answers=answers, raw_tokens=10)


def test_align_candidates_duplicate_join_keys_keep_the_first():
    """Two candidates sharing a domain: the producer's FIRST-ranked
    candidate owns the pair — ONE request for the key, the first verdict
    stands, and the duplicate costs nothing (no request, no row) and never
    gets stamped with a later verdict of its own."""
    candidates = [
        {"domain": "dup.com", "label": "First"},
        {"domain": "dup.com", "label": "Second"},
        {"domain": "solo.com", "label": "Solo"},
    ]
    decider = _OrderSensitiveDecider()
    ledger = DecideLedger()
    out = align_candidates({"name": "Acme"}, candidates, decider, {}, ledger,
                           RUN, "enforce")
    assert decider.n_calls == 2  # the duplicate spent NOTHING
    assert len(ledger.rows) == 2
    assert set(out) == {"dup.com", "solo.com"}
    # dup.com carries the FIRST candidate's verdict (1.4 -> related), not
    # the would-be second call's.
    assert out["dup.com"]["score"] == 1.4
    assert out["dup.com"]["outcome"] == "related"
    assert out["solo.com"]["score"] == 0.2


def test_align_candidates_unavailable_pairs_never_join_the_merge():
    """Only BINDING alignments come back: a shadow run or an errored pair
    is rows-only, so the caller merges nothing for it."""
    candidates = _candidates(2)
    decider = _link_decider(ok=False)
    out = align_candidates({"name": "Acme"}, candidates, decider, {}, DecideLedger(),
                           RUN, "enforce")
    assert out == {}
    assert decider.n_calls == 2  # the requests still ran (rows recorded)

    shadow = _link_decider()
    out = align_candidates({"name": "Acme"}, _candidates(2), shadow, {},
                           DecideLedger(), RUN, "shadow")
    assert out == {}


def test_align_candidates_empty_or_keyless_input_is_a_no_op():
    decider = _link_decider()
    assert align_candidates({"name": "Acme"}, [], decider, {}, DecideLedger(), RUN,
                            "enforce") == {}
    assert align_candidates({"name": "Acme"}, None, decider, {}, DecideLedger(), RUN,
                            "enforce") == {}
    assert decider.n_calls == 0


# ── the join-key helper (producer-agnostic) ─────────────────────────────────


def test_candidate_key_prefers_domain_then_url_then_name():
    assert candidate_key({"domain": "a.com", "url": "u", "name": "n"}) == "a.com"
    # Competitor rows carry no domain: the article url joins them.
    assert candidate_key({"name": "paypal", "url": "https://x/1"}) == "https://x/1"
    assert candidate_key({"name": "paypal"}) == "paypal"
    assert candidate_key({"score": 1}) is None
    assert candidate_key({}) is None
    assert candidate_key({"domain": "   "}) is None
