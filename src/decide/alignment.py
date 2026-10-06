"""Entity alignment: per-candidate Jev annotation for the discover frames.

Waves-2/3 Task 7. The discover sweep ranks identity candidates (the
``src/identity/discover.py`` waterfall, ``CompetitorNewsPass``) and queues
them for HUMAN review; what the queue lacked is EVIDENCE — which of the
ranked candidates plausibly IS the queried company and, when it is not,
which field broke the tie. This module is that annotation pass, on the
entity-alignment cookbook's core mechanic:

- ONE Jev request per (mention, candidate) pair: a single Score question
  ``link_state`` with THREE ordered levels whose wording IS the outcome
  (0 = "different company"; 1 = "closely related, may or may not be the
  same — a subsidiary, parent, similarly-named company, or a product line
  that could plausibly refer to either"; 2 = "one and the same company"),
  plus FOUR companion Nouls in the SAME request (same_name, same_domain,
  same_location, same_industry) — the curator's per-field disagreement
  evidence: when the score lands on the middle level, the fields show
  WHICH comparison drove it there.
- Routing = ROUND TO THE NEAREST LEVEL: ``min(int(score + 0.5), 2)`` -> one
  of ["different", "related", "same"]. NO thresholds — the middle level's
  wording is the TUNING KNOB (narrow it and borderline pairs drift to
  "different"; widen it and they drift to "same"). The half edge pins UP
  (0.5 -> related, 1.5 -> same); extremes clamp at both ends. Numeric
  comparisons are code's job — none are asked of the decider here.
- The annotation is ENFORCE-ONLY. ``align_pair`` returns the routed
  outcome only in enforce mode; in shadow (or any non-enforce mode — the
  gates.py fail-safe-inert convention) the pair still RUNS and the ledger
  row records the would-be annotation, but the returned dict is the
  unavailable shape, so the caller's queue stays byte-identical.

Never raises past the seam (the jev.py contract, belt-and-braces caught):
NullDecider / not-applicable, a Jev error, or a malformed score all return
``{"outcome": "unavailable", "score": None, "fields": {}}``. Ledger:
boundary "entity_alignment", ONE row per pair (answers carry the score
question and all four field nouls; ``agree`` stays None at this boundary —
there is no deterministic baseline to agree with, the ``error`` column
carries the failure signal).

No network anywhere in this module except through the injected decider.
"""

from __future__ import annotations

import math

from loguru import logger

from src.core.textutil import truncate
from src.decide.gates import DecideLedger
from src.decide.gates import _call, _noul_of, _row  # single-source row conventions

__all__ = ["MAX_ALIGN_PAIRS", "align_candidates", "align_pair", "candidate_key"]

# Default pair cap per discover run (config/decide.yaml
# ``decider.gates.entity_alignment.max_pairs`` overrides).
MAX_ALIGN_PAIRS = 10

# The three ordered levels: the WORDING IS the outcome. The middle level is
# the tuning knob (see module docstring) — do not add numeric thresholds.
_LINK_STATE_LEVELS = (
    "different company",
    "closely related, may or may not be the same — a subsidiary, parent, "
    "similarly-named company, or a product line that could plausibly refer to either",
    "one and the same company",
)
_OUTCOMES = ("different", "related", "same")

_LINK_STATE_INSTRUCTIONS = (
    "Score how the MENTION company and the CANDIDATE company relate using the "
    "ordered criteria levels (0 = first level, 1 = second, 2 = third)."
)

# The four companion Nouls, judged in the SAME request as link_state. TRUE =
# the field supports "same company"; the per-field values are the curator's
# disagreement evidence (which field broke the tie), never a gate.
_FIELD_NOULS: dict[str, str] = {
    "same_name": (
        "TRUE = the two names refer to the same company name. "
        "FALSE = they name different companies."
    ),
    "same_domain": (
        "TRUE = the two domains/websites belong to the same organization. "
        "FALSE = they belong to different organizations."
    ),
    "same_location": (
        "TRUE = the two locations are consistent with the same company. "
        "FALSE = the locations point to different companies."
    ),
    "same_industry": (
        "TRUE = the two industry descriptions are consistent with the same "
        "company. FALSE = they describe different industries."
    ),
}

_ALIGNMENT_FIELDS = tuple(_FIELD_NOULS)

# Evidence-ish text in the state is capped at the _ANSWER_TEXT_LIMIT habit
# (gates.py); a snippet is context, not a document.
_SNIPPET_CHARS = 400

# The unavailable posture: every failure mode degrades to this shape.
_UNAVAILABLE = {"outcome": "unavailable", "score": None, "fields": {}}


def _mention_state(mention: dict | None) -> dict:
    """The mention-side profile, built defensively (missing keys => None/"")."""
    m = mention or {}
    return {
        "name": m.get("name"),
        "domain": m.get("domain"),
        "location": m.get("location"),
        "industry": m.get("industry"),
        "snippet": truncate(str(m.get("snippet") or ""), _SNIPPET_CHARS),
    }


def _company_state(company: dict | None) -> dict:
    """The candidate-side profile, built defensively (missing keys => None)."""
    c = company or {}
    return {
        "name": c.get("name"),
        "domain": c.get("domain"),
        "hq": c.get("hq"),
        "industry": c.get("industry"),
    }


def _questions() -> dict[str, dict]:
    """The ONE request's five questions: one Score + four Nouls."""
    questions: dict[str, dict] = {
        "link_state": {
            "type": "score",
            "instructions": _LINK_STATE_INSTRUCTIONS,
            "criteria": list(_LINK_STATE_LEVELS),
        }
    }
    for field, instructions in _FIELD_NOULS.items():
        questions[field] = {"type": "noul", "instructions": instructions}
    return questions


def _route(score: float) -> str:
    """Round to the NEAREST level (half-up via +0.5 before int truncation),
    clamped to the level range. The cookbook's core mechanic: no thresholds."""
    index = max(0, min(int(score + 0.5), len(_OUTCOMES) - 1))
    return _OUTCOMES[index]


def _score_of(answer: object) -> float | None:
    """The numeric ``score`` of a link_state answer (None when absent,
    non-numeric, boolean, or non-finite — NaN poisons JSON ledgers)."""
    if isinstance(answer, dict):
        value = answer.get("score")
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        ):
            return float(value)
    return None


def candidate_key(candidate: dict | None) -> str | None:
    """The pair's join key back into its own candidate list: ``domain``
    first (the waterfall's key), then ``url`` / ``name`` so the producer-
    agnostic competitor rows (no domain; the url is the co-mention article)
    join too. None when the dict carries none of the three — the candidate
    is skipped without spending budget."""
    cand = candidate or {}
    for key in ("domain", "url", "name"):
        value = cand.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _company_of(candidate: dict | None) -> dict:
    """The candidate as the request's ``company`` profile. Name preference:
    ``label`` (waterfall display names) then ``name`` (competitor rows,
    where ``title`` is the co-mention headline — never the company name)
    then ``title`` (ddg SERP titles). Nothing is fabricated: hq/industry
    ride only when a producer supplied them."""
    cand = candidate or {}
    name = cand.get("label") or cand.get("name") or cand.get("title")
    return {
        "name": name,
        "domain": cand.get("domain"),
        "hq": cand.get("hq"),
        "industry": cand.get("industry"),
    }


def _max_pairs(gate_cfg: dict | None) -> int:
    """``max_pairs`` from the gate block (default :data:`MAX_ALIGN_PAIRS`);
    a bad value warns and uses the default (the gates.py ``_threshold``
    habit). Zero or negative disables the pass."""
    raw = (gate_cfg or {}).get("max_pairs", MAX_ALIGN_PAIRS)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        logger.warning(
            "entity alignment: bad max_pairs {!r}; using default {}", raw, MAX_ALIGN_PAIRS
        )
        return MAX_ALIGN_PAIRS


def align_pair(
    mention: dict | None,
    company: dict | None,
    decider,
    gate_cfg: dict | None,
    ledger: DecideLedger,
    run_id: str,
    mode: str,
) -> dict:
    """Align ONE (mention, candidate-company) pair with ONE Jev request.

    ``gate_cfg`` is the decide.yaml ``entity_alignment`` block; ``on_error``
    is accepted for config consistency, but skip is the only posture an
    annotation can take — every failure degrades to the unavailable shape
    rather than binding. ``mode`` is "enforce" | anything else (fail-safe
    inert, the gates.py convention).

    Returns ``{"outcome", "score", "fields"}``: the routed outcome plus the
    four per-field noul values (None where the decider gave no usable noul)
    — or the unavailable shape for every not-binding posture. The ledger
    records ONE "entity_alignment" row per pair either way.
    """
    state = {"mention": _mention_state(mention), "company": _company_state(company)}
    enforce = mode == "enforce"
    try:
        decision, latency_ms = _call(decider, state, _questions())
    except Exception as exc:  # the protocol says never; audit.py catches too
        logger.warning("entity alignment: decider raised (run={}): {}", run_id, exc)
        ledger.record(
            _row(
                "entity_alignment", state, decider, run_id,
                answers=None, deterministic_action="skip", agree=None,
                agree_direction=None, error="jev_error", latency_ms=0.0,
                raw_tokens=0, called=False, outcome="errored",
            )
        )
        return dict(_UNAVAILABLE)

    called = bool(decision.applies)
    raw_tokens = int(decision.raw_tokens or 0)
    if not decision.applies:
        # Not applicable (NullDecider / unmatched mock): the row records
        # nothing judged and inflates no aggregate counts (house: no outcome).
        ledger.record(
            _row(
                "entity_alignment", state, decider, run_id,
                answers=None, deterministic_action="skip", agree=None,
                agree_direction=None, error=None, latency_ms=latency_ms,
                raw_tokens=raw_tokens, called=False,
            )
        )
        return dict(_UNAVAILABLE)

    if not decision.ok:
        # on_error posture: no annotation, the deterministic candidates stand;
        # the row records the error (outcome "errored" feeds the aggregates).
        ledger.record(
            _row(
                "entity_alignment", state, decider, run_id,
                answers=None, deterministic_action="skip", agree=None,
                agree_direction=None, error="jev_error", latency_ms=latency_ms,
                raw_tokens=raw_tokens, called=called, outcome="errored",
            )
        )
        return dict(_UNAVAILABLE)

    answers = decision.answers if isinstance(decision.answers, dict) else {}
    score = _score_of(answers.get("link_state"))
    if score is None:
        ledger.record(
            _row(
                "entity_alignment", state, decider, run_id,
                answers=dict(answers), deterministic_action="skip", agree=None,
                agree_direction=None, error="invalid_score", latency_ms=latency_ms,
                raw_tokens=raw_tokens, called=called, outcome="unavailable",
            )
        )
        return dict(_UNAVAILABLE)

    outcome = _route(score)
    fields = {field: _noul_of(answers, field) for field in _ALIGNMENT_FIELDS}
    ledger.record(
        _row(
            "entity_alignment", state, decider, run_id,
            answers=dict(answers), deterministic_action="skip", agree=None,
            agree_direction=None, error=None, latency_ms=latency_ms,
            raw_tokens=raw_tokens, called=called, outcome=outcome,
        )
    )
    if not enforce:
        # Shadow: the row above carries the would-be annotation (the shadow
        # audit tunes from it); the caller's queue stays untouched.
        return dict(_UNAVAILABLE)
    return {"outcome": outcome, "score": score, "fields": fields}


def align_candidates(
    mention: dict | None,
    candidates: list[dict] | None,
    decider,
    gate_cfg: dict | None,
    ledger: DecideLedger,
    run_id: str,
    mode: str,
) -> dict[str, dict]:
    """Align one mention against up to ``max_pairs`` candidates, IN LIST
    ORDER (the producer's ranking — the top pairs get the budget).

    The cap counts REQUESTS: every candidate with a join key (see
    :func:`candidate_key`) costs one attempt until the cap is reached;
    join-key-less candidates are skipped without consuming budget.

    Returns ``{join key: alignment}`` carrying ONLY alignments that bind
    (enforce mode AND a routed verdict). Everything else — layer inert,
    Jev error, malformed score, the shadow posture — is rows-only, so the
    caller merges exactly what is returned and a shadow run's queue stays
    byte-identical.
    """
    cfg = dict(gate_cfg or {})
    cap = _max_pairs(cfg)
    aligned: dict[str, dict] = {}
    if cap <= 0:
        return aligned
    attempts = 0
    for cand in candidates or []:
        if attempts >= cap:
            break
        key = candidate_key(cand)
        if key is None:
            continue
        attempts += 1
        result = align_pair(
            mention, _company_of(cand), decider, cfg, ledger, run_id, mode
        )
        if result["outcome"] in _OUTCOMES:
            aligned[key] = result
    return aligned
