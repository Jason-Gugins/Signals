"""The five decide-layer gates + the per-run DecideLedger (Task 4).

One function per gate boundary. Each gate: builds a token-budgeted ``state``
from repo objects, makes ONE batched Jev call through the injected decider,
applies the configured floor + ``on_error`` rule, and emits a ledger row
(invariant-5 shape) plus auto-maintained aggregate evidence for the run.

Mode semantics (``mode`` is "shadow" | "enforce"; anything else is treated as
shadow — fail-safe inert):

- **enforce** — the verdict binds, subject to the gate floor and the
  ``on_error`` rule from ``config/decide.yaml`` (``decider.gates.<name>``).
- **shadow** — the gate is INERT: no Jev verdict ever binds. G3/G5 return
  the deterministic / no-gate outcome, G4 applies only its deterministic
  machinery (doc existence + prefilter), and G1 returns the WOULD-BE
  enforce action so the caller can surface it — in every case the row
  records what enforce WOULD do (the would-be outcome rides in
  ``outcome``/``answers``/``noul`` so the shadow audit can tune floors
  before enforce ever binds). Binding is the CALLER's mode decision
  (``run_intel`` applies a G1 action only in enforce mode).

Row semantics pinned across gates (invariant 5):

- ``deterministic_action`` — what happens WITHOUT gating: the deterministic
  baseline for that boundary (G1 "drop_llm" = the deterministic plan default;
  G4 "accept" = implementer claims are additive; G5 the caller's
  ``deterministic_promote``; G3 the heuristic; G2 "proceed_deterministic").
- ``agree`` — compares the Jev-derived outcome against that deterministic
  baseline (None when no verdict exists; False on an enforce on_error
  fallback). In enforce mode ``agree: false + error: null`` therefore means
  the floor rejected content the deterministic path would have kept — a Jev
  failure shows up as ``agree: false + error`` set instead.
- ``agree_direction`` — directional labels are specified only for G5
  (``match`` | ``jev_yes_det_no`` | ``jev_no_det_yes``); the other gates set
  ``match`` on agreement and None otherwise (the boolean ``agree`` carries
  the disagreement signal).
- Not-applicable (NullDecider or an unmatched mock verdict, ``applies=False``)
  fails safe: G1/G4 drop the LLM content (fail-closed — the deterministic
  dossier stands alone), G3/G5 return the deterministic choice. Rows are
  still recorded (invariant 4) with a None answer and ``called=False``, and
  inflate no aggregate counts.

G4 batching: one Jev request per document with a named state field per
claim (``claim_<i>`` — ``i`` indexes the FULL original claim list so ids are
stable). The single call's ``called``/``raw_tokens``/``latency_ms`` are
attributed to the FIRST judged row so per-gate aggregates count requests and
tokens once, not once per claim.

G4 deterministic stages bind in EVERY mode — only the Jev floor is
shadow-inert. A missing document or a sub-floor lexical overlap drops claims
in shadow too; they are deterministic machinery, not Jev verdicts.

No network anywhere in this module except through the injected decider.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from loguru import logger

from src.core.textutil import truncate
from src.decide.shapes import Claim, Decision, PlanStep

# Floors + threshold: module-constant defaults so missing config keys are safe.
PLAN_FLOOR = 0.70
CLAIM_FLOOR = 0.70
PROMOTION_FLOOR = 0.70
ROUTE_THRESHOLD = 2000

# Calibration band low edge (G4/G5): noul in [band_low, floor) routes to a
# recorded review outcome instead of a hard drop.
_BAND_LOW = 0.30

# Lexical-overlap prefilter floor (G4 stage b): claims sharing fewer of their
# word tokens with the cited document than this are dropped BEFORE any Jev
# call — a cheap catch for plausible-but-uncited confabulations.
LEXICAL_OVERLAP_FLOOR = 0.15

# G3 tie band: within +/-20% of route_threshold Jev breaks the tie; outside
# it the heuristic fires without a call.
ROUTE_TIE_BAND = 0.20

# Anchor-window sizing (chars-per-token mirrors estimate_tokens' 4-chars/1-token).
HEAD_TOKENS = 100
_WINDOW_CHARS_PER_TOKEN = 4

# Detail-row text fields are capped at this many chars — the same habit as
# the dossier's EVIDENCE_TEXT_LIMIT (src/export/intel_package.py).
_ANSWER_TEXT_LIMIT = 400

_TOKEN_RE = re.compile(r"[a-z0-9]+")


# --- shared helpers -----------------------------------------------------------


def estimate_tokens(text: str) -> int:
    """Cheap token estimate: ~4 chars per token, never zero."""
    return max(1, len(text) // 4)


def state_hash(state) -> str:
    """sha256 of the normalized state — lets a re-run spot identical inputs."""
    payload = json.dumps(state, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def lexical_overlap(claim_text: str, doc_text: str) -> float:
    """Lowercase word-token overlap: |claim ∩ doc| / max(1, |claim tokens|)."""
    claim = set(_TOKEN_RE.findall((claim_text or "").lower()))
    if not claim:
        return 0.0
    doc = set(_TOKEN_RE.findall((doc_text or "").lower()))
    return len(claim & doc) / max(1, len(claim))


def _normalize_with_map(text: str) -> tuple[str, list[int]]:
    """Casefolded, whitespace-collapsed copy of ``text`` plus a map from each
    normalized char index back to its ORIGINAL char index (casefold can
    expand one char into many; every emitted char gets its source index, so
    the map stays aligned with the normalized string)."""
    chars: list[str] = []
    idx: list[int] = []
    pending_space = False
    for i, ch in enumerate(text):
        if ch.isspace():
            # Collapse whitespace runs to one space; no leading space.
            if chars and not pending_space:
                chars.append(" ")
                idx.append(i)
                pending_space = True
            continue
        pending_space = False
        for folded in ch.casefold():
            chars.append(folded)
            idx.append(i)
    return "".join(chars), idx


def anchor_window(doc_text: str, anchor: str, window_tokens: int = 300) -> str:
    """Head excerpt + the excerpt centered on ``anchor`` in ``doc_text``.

    Document heads are frequently nav/boilerplate and claims are supported by
    mid-page content, so head-only state systematically drops legitimate
    claims. The anchor is located via normalized substring match (casefold +
    whitespace-collapsed); when found the window spans +/-``window_tokens``
    around the match (~600 tokens total at the 4-chars/token estimate) on top
    of a ~100-token head. When the anchor is NOT found the state falls back
    to the head (``textutil.truncate`` word-boundary cut, the house
    EVIDENCE_TEXT_LIMIT habit).
    """
    if not doc_text:
        return ""
    head = truncate(doc_text, HEAD_TOKENS * _WINDOW_CHARS_PER_TOKEN) or ""
    if not anchor or not anchor.strip():
        return head
    norm_doc, idx = _normalize_with_map(doc_text)
    norm_anchor = " ".join(anchor.split()).casefold()
    pos = norm_doc.find(norm_anchor)
    if pos < 0:
        return head
    start_orig = idx[pos]
    end_orig = idx[min(pos + len(norm_anchor) - 1, len(idx) - 1)] + 1
    half = max(1, window_tokens) * _WINDOW_CHARS_PER_TOKEN
    window = doc_text[max(0, start_orig - half) : min(len(doc_text), end_orig + half)]
    if not window:
        return head
    return f"{head}\u2026{window}"


class DecideLedger:
    """In-memory per-run decision ledger (Task 7 writes decisions.jsonl and
    feeds the aggregates to the dossier).

    ``rows`` keeps every recorded row verbatim; aggregates are auto-maintained
    per gate name (one aggregate per BOUNDARY for the whole run, not per
    document) from row conventions:

    - ``called`` truthy  -> the Jev call counter (requests, not claims).
    - ``outcome`` of "accepted" | "dropped" | "errored" -> the matching count.
    - numeric ``noul``   -> the mean-noul sample.
    - ``raw_tokens``     -> summed as ``input_tokens``.
    - ``detail``         ONLY rows for dropped/errored/disagreed content,
      each a capped copy (string fields truncated to 400 chars, mirroring the
      repo's EVIDENCE_TEXT_LIMIT habit).

    Rows where Jev never applied (NullDecider, unmatched mock: ``called``
    falsy, ``outcome`` absent, ``agree`` None) are recorded but inflate none
    of the counts — documented choice: "called" means Jev actually ran.
    """

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self._agg: dict[str, dict] = {}

    def _agg_for(self, gate: str) -> dict:
        return self._agg.setdefault(
            gate,
            {
                "called": 0,
                "accepted": 0,
                "dropped": 0,
                "errored": 0,
                "noul_sum": 0.0,
                "noul_n": 0,
                "raw_tokens": 0,
                "detail": [],
            },
        )

    def record(self, row: dict) -> None:
        """Append one row and fold it into the per-gate aggregates."""
        row = dict(row)
        self.rows.append(row)
        agg = self._agg_for(str(row.get("gate", "unknown")))
        if row.get("called"):
            agg["called"] += 1
        outcome = row.get("outcome")
        if outcome in ("accepted", "dropped", "errored"):
            agg[outcome] += 1
        noul = row.get("noul")
        if isinstance(noul, (int, float)) and not isinstance(noul, bool):
            agg["noul_sum"] += float(noul)
            agg["noul_n"] += 1
        raw = row.get("raw_tokens") or 0
        if isinstance(raw, (int, float)):
            agg["raw_tokens"] += int(raw)
        if outcome in ("dropped", "errored") or row.get("agree") is False:
            agg["detail"].append(_bounded_row(row))

    def aggregate(self, gate: str) -> dict:
        """The run-level evidence record for one gate boundary."""
        agg = self._agg.get(gate)
        if agg is None:
            return {
                "gate": gate,
                "counts": {"called": 0, "accepted": 0, "dropped": 0, "errored": 0},
                "mean_noul": None,
                "input_tokens": 0,
                "detail": [],
            }
        return {
            "gate": gate,
            "counts": {
                "called": agg["called"],
                "accepted": agg["accepted"],
                "dropped": agg["dropped"],
                "errored": agg["errored"],
            },
            "mean_noul": (agg["noul_sum"] / agg["noul_n"]) if agg["noul_n"] else None,
            "input_tokens": agg["raw_tokens"],
            "detail": list(agg["detail"]),
        }

    def write(self, path) -> None:
        """Append every row as one JSON line (utf-8), creating parent dirs."""
        p = Path(path)
        if str(p.parent):
            p.parent.mkdir(parents=True, exist_ok=True)
        # newline="\n" keeps the JSONL LF-terminated on Windows too (the same
        # habit as write_intel_package).
        with p.open("a", encoding="utf-8", newline="\n") as fh:
            for row in self.rows:
                fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


# --- row plumbing --------------------------------------------------------------


def _bounded_row(row: dict) -> dict:
    """Capped copy of a row for aggregate detail: string fields (incl. inside
    dicts/lists) truncated to 400 chars; other scalars ride through."""

    def cap(value):
        if isinstance(value, str):
            return value[:_ANSWER_TEXT_LIMIT]
        if isinstance(value, dict):
            return {k: cap(v) for k, v in value.items()}
        if isinstance(value, list):
            return [cap(v) for v in value]
        return value

    return {k: cap(v) for k, v in row.items()}


def _model_name(decider) -> str:
    return getattr(decider, "model", None) or type(decider).__name__


def _row(
    gate: str,
    state,
    decider,
    run_id: str,
    *,
    answers,
    deterministic_action,
    agree,
    agree_direction,
    error,
    latency_ms,
    raw_tokens,
    called,
    outcome=None,
    noul=None,
    reason=None,
) -> dict:
    """One invariant-5 ledger row (gates may add the called/outcome/noul/
    reason extras the aggregates read)."""
    row = {
        "gate": gate,
        "boundary": gate,  # one boundary tag per gate in v1
        "state_hash": state_hash(state),
        "answers": answers,
        "deterministic_action": deterministic_action,
        "agree": agree,
        "agree_direction": agree_direction,
        "error": error,
        "latency_ms": latency_ms,
        "raw_tokens": raw_tokens,
        "model": _model_name(decider),
        "run_id": run_id,
    }
    row["called"] = bool(called)
    if outcome is not None:
        row["outcome"] = outcome
    if noul is not None:
        row["noul"] = noul
    if reason is not None:
        row["reason"] = reason
    return row


def _call(decider, state, questions) -> tuple[Decision, float]:
    """Time one decider invocation; never raises past the seam (jev.py's
    contract collapses failures into ok=False Decisions)."""
    t0 = time.perf_counter()
    decision = decider.decide(state, questions)
    latency_ms = round((time.perf_counter() - t0) * 1000.0, 3)
    return decision, latency_ms


def _cfg(gate_cfg: dict | None) -> dict:
    return gate_cfg or {}


def _floor(gate_cfg: dict | None, default: float) -> float:
    try:
        return float((gate_cfg or {}).get("floor", default))
    except (TypeError, ValueError):
        logger.warning(
            "decide layer: bad floor {!r}; using default {}", (gate_cfg or {}).get("floor"), default
        )
        return default


def _noul_of(answers, qid: str) -> float | None:
    ans = (answers or {}).get(qid)
    if isinstance(ans, dict):
        value = ans.get("noul")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _choice_of(answers, qid: str) -> str | None:
    ans = (answers or {}).get(qid)
    if isinstance(ans, dict):
        value = ans.get("choice")
        if isinstance(value, str):
            return value.strip().lower()
    return None


# --- G1: plan qualification -----------------------------------------------------


def gate_plan_step(
    step: PlanStep,
    icp_excerpt: str,
    decider,
    gate_cfg: dict | None,
    ledger: DecideLedger,
    run_id: str,
    mode: str,
) -> tuple[str, Decision]:
    """G1 — qualify one planned step against the ICP (plan -> collect).

    noul >= floor keeps the LLM plan override standing; below the floor
    (or a Jev error / no decider) the step falls back to its DETERMINISTIC
    default ordering/budget — the step is NOT skipped entirely. Returns
    (action, decision); the caller (Task 7) applies the action.

    EVERY mode returns the would-be enforce action ("keep" only when the
    verdict meets the floor) so rows and artifacts always show what enforce
    would do; what differs is BINDING: an enforce caller applies the action,
    a shadow caller must NOT (invariant 5 — shadow never binds the plan).
    The row's inertness semantics are unchanged: ``deterministic_action``
    stays the no-gate default ("drop_llm") and ``agree`` compares the
    verdict against that baseline.
    """
    cfg = _cfg(gate_cfg)
    floor = _floor(cfg, PLAN_FLOOR)
    enforce = mode == "enforce"
    state = {
        "icp_excerpt": icp_excerpt,
        "step": {
            "source_ids": list(step.source_ids),
            "budget_knobs": dict(step.budget_knobs),
            "acceptance_criteria": list(step.acceptance_criteria),
        },
    }
    questions = {
        "step": {
            "type": "noul",
            "instructions": (
                "This step targets information the ICP config says "
                "matters for this account. Answer noul."
            ),
        }
    }
    # Without gating the deterministic plan default stands; only a qualified
    # verdict lets the LLM override bind (narrow-only: never the reverse).
    det_action = "drop_llm"

    decision, latency_ms = _call(decider, state, questions)
    called = bool(decision.applies)
    raw_tokens = int(decision.raw_tokens or 0)

    if not decision.applies:
        # Fail-safe: no verdict -> deterministic default (narrow-only).
        ledger.record(
            _row(
                "plan_qualification", state, decider, run_id,
                answers=None, deterministic_action=det_action, agree=None,
                agree_direction=None, error=None, latency_ms=latency_ms,
                raw_tokens=raw_tokens, called=called,
            )
        )
        return "drop_llm", decision

    if not decision.ok:
        # on_error drop_llm (and use_deterministic: both hand the step back to
        # its deterministic default) — remaining steps are unaffected.
        ledger.record(
            _row(
                "plan_qualification", state, decider, run_id,
                answers=None, deterministic_action=det_action,
                agree=False if enforce else None, agree_direction=None,
                error="jev_error", latency_ms=latency_ms, raw_tokens=raw_tokens,
                called=called, outcome="errored",
            )
        )
        logger.debug("G1 plan step errored; falling back to deterministic default (run={})", run_id)
        return "drop_llm", decision

    noul = _noul_of(decision.answers, "step")
    would = "keep" if (noul is not None and noul >= floor) else "drop_llm"
    agree = None if noul is None else would == det_action
    direction = "match" if agree else None
    # Both modes return the WOULD-BE enforce action so artifacts show what
    # enforce would do; only an enforce CALLER binds it (Task 7 applies the
    # plan override in enforce mode alone — shadow never binds the plan).
    action, outcome = would, ("accepted" if would == "keep" else "dropped")
    ledger.record(
        _row(
            "plan_qualification", state, decider, run_id,
            answers=dict(decision.answers), deterministic_action=det_action,
            agree=agree, agree_direction=direction, error=None,
            latency_ms=latency_ms, raw_tokens=raw_tokens, called=called,
            outcome=outcome, noul=noul,
        )
    )
    return action, decision


# --- G2: posture audit (shadow-only by construction) ----------------------------


def gate_posture_audit(
    source,
    fetch_scope_desc: str,
    decider,
    gate_cfg: dict | None,
    ledger: DecideLedger,
    run_id: str,
    mode: str,
) -> Decision:
    """G2 — audit one fetch scope against the source's declared posture.

    SHADOW-ONLY in v1: the verdict is logged and the Decision returned;
    NOTHING binds. The deterministic posture machinery (robots, cadence,
    fanout, requires, runner cursors) IS the gate — a Jev gate here could
    only stall fetches the deterministic posture already permits, turning a
    Jev outage into a collection outage. There is deliberately NO code path
    that alters fetch behavior, in any mode (``mode`` is accepted for
    signature uniformity across gates only).
    """
    state = {"source": source, "fetch_scope": fetch_scope_desc}
    questions = {
        "posture": {
            "type": "choice",
            "instructions": "Was this fetch scope within the source's declared posture?",
            "criteria": ["within_posture", "outside_posture"],
        }
    }
    decision, latency_ms = _call(decider, state, questions)
    called = bool(decision.applies)
    answers = dict(decision.answers) if (called and decision.ok) else None
    agree = direction = error = None
    if called and decision.ok:
        choice = _choice_of(decision.answers, "posture")
        if choice is None:
            error = "invalid_choice"
        else:
            # The deterministic path proceeded -> "within_posture" agrees.
            agree = choice == "within_posture"
            direction = "match" if agree else None
    elif called:
        error = "jev_error"
    ledger.record(
        _row(
            "posture_audit", state, decider, run_id,
            answers=answers, deterministic_action="proceed_deterministic",
            agree=agree, agree_direction=direction, error=error,
            latency_ms=latency_ms, raw_tokens=int(decision.raw_tokens or 0),
            called=called,
        )
    )
    return decision


# --- G3: implementer routing -----------------------------------------------------


def gate_routing(
    doc_tokens: int,
    decider,
    gate_cfg: dict | None,
    ledger: DecideLedger,
    run_id: str,
    mode: str,
) -> str:
    """G3 — deep-context extraction vs quick classification (per implement call).

    The deterministic heuristic fires FIRST: ``doc_tokens >= route_threshold``
    -> "deep" else "quick". Only a document within +/-20% of the threshold
    (the tie band, edges inclusive) consults Jev; outside the band no call is
    made at all. On a Jev error (``on_error: use_deterministic``) or a
    malformed verdict the heuristic choice stands. Returns "deep" | "quick".
    """
    cfg = _cfg(gate_cfg)
    enforce = mode == "enforce"
    try:
        threshold = int(cfg.get("route_threshold", ROUTE_THRESHOLD))
    except (TypeError, ValueError):
        logger.warning(
            "decide layer: bad route_threshold {!r}; using default {}",
            cfg.get("route_threshold"),
            ROUTE_THRESHOLD,
        )
        threshold = ROUTE_THRESHOLD
    heuristic = "deep" if doc_tokens >= threshold else "quick"
    in_band = abs(doc_tokens - threshold) <= ROUTE_TIE_BAND * threshold
    state = {
        "doc_tokens": int(doc_tokens),
        "route_threshold": threshold,
        "heuristic": heuristic,
        "in_tie_band": in_band,
    }

    if not in_band:
        # Outside the band Jev is NOT called; the row records the heuristic.
        ledger.record(
            _row(
                "routing", state, decider, run_id,
                answers=None, deterministic_action=heuristic, agree=None,
                agree_direction=None, error=None, latency_ms=0.0,
                raw_tokens=0, called=False,
            )
        )
        return heuristic

    questions = {
        "routing": {
            "type": "choice",
            "instructions": "Deep-context extraction vs quick classification?",
            "criteria": ["deep", "quick"],
        }
    }
    decision, latency_ms = _call(decider, state, questions)
    called = bool(decision.applies)
    raw_tokens = int(decision.raw_tokens or 0)

    if not decision.applies:
        ledger.record(
            _row(
                "routing", state, decider, run_id,
                answers=None, deterministic_action=heuristic, agree=None,
                agree_direction=None, error=None, latency_ms=latency_ms,
                raw_tokens=raw_tokens, called=False,
            )
        )
        return heuristic

    if not decision.ok:
        ledger.record(
            _row(
                "routing", state, decider, run_id,
                answers=None, deterministic_action=heuristic,
                agree=False if enforce else None, agree_direction=None,
                error="jev_error", latency_ms=latency_ms, raw_tokens=raw_tokens,
                called=called,
            )
        )
        return heuristic  # on_error: use_deterministic (fail-safe for any other value)

    choice = _choice_of(decision.answers, "routing")
    if choice not in ("deep", "quick"):
        ledger.record(
            _row(
                "routing", state, decider, run_id,
                answers=dict(decision.answers), deterministic_action=heuristic,
                agree=None, agree_direction=None, error="invalid_choice",
                latency_ms=latency_ms, raw_tokens=raw_tokens, called=called,
            )
        )
        return heuristic
    agree = choice == heuristic
    ledger.record(
        _row(
            "routing", state, decider, run_id,
            answers=dict(decision.answers), deterministic_action=heuristic,
            agree=agree, agree_direction="match" if agree else None, error=None,
            latency_ms=latency_ms, raw_tokens=raw_tokens, called=called,
        )
    )
    # Enforce: the verdict breaks the tie. Shadow: the heuristic stands.
    return (choice if enforce else heuristic)


# --- G4: citation soundness -------------------------------------------------------


def gate_citation_batch(
    claims: list[Claim],
    doc_id: str,
    doc_text: str | None,
    decider,
    gate_cfg: dict | None,
    ledger: DecideLedger,
    run_id: str,
    mode: str,
) -> tuple[list[Claim], Decision]:
    """G4 — citation soundness for one document's claim batch.

    Sequence: (a) deterministic doc existence (the caller supplies
    ``doc_text``; None/empty drops every claim BEFORE Jev), (b) a per-claim
    lexical-overlap prefilter (below :data:`LEXICAL_OVERLAP_FLOOR` drops
    before Jev), (c) ONE batched Jev request for the survivors — state
    ``{"doc_id": ..., "claim_<i>": anchor_window(...)}`` with one Noul
    question per claim, ids indexing the FULL original list so they are
    stable — then (d) the floor.

    Stages (a)+(b) are DETERMINISTIC machinery and bind in every mode; only
    the Jev floor is shadow-inert (shadow returns all judged claims and rows
    record the would-be outcome). Enforce drops below-floor claims (reason
    "floor", into the aggregate detail); a Jev error drops ALL batch claims —
    implementer output is additive, so the outage degrades to exactly today's
    dossier. Not-applicable (NullDecider) fails closed for LLM content too.

    Returns (accepted_claims, decision).
    """
    cfg = _cfg(gate_cfg)
    floor = _floor(cfg, CLAIM_FLOOR)
    enforce = mode == "enforce"
    not_applicable = Decision(applies=False, ok=True, answers={}, raw_tokens=0)

    def _drop_row(i: int, claim: Claim, reason: str) -> None:
        # The state that WOULD have been sent — audit value for the detail.
        state = {
            "doc_id": doc_id,
            f"claim_{i}": anchor_window(doc_text, claim.text) if doc_text else claim.text,
        }
        ledger.record(
            _row(
                "citation_soundness", state, decider, run_id,
                answers=None, deterministic_action="accept", agree=None,
                agree_direction=None, error=None, latency_ms=0.0, raw_tokens=0,
                called=False, outcome="dropped", reason=reason,
            )
        )

    # (a) deterministic doc existence — before any Jev spend.
    if not doc_text:
        for i, claim in enumerate(claims):
            _drop_row(i, claim, "no_document")
        return [], not_applicable

    # (b) lexical-overlap prefilter — before any Jev spend.
    survivors: list[tuple[int, Claim]] = []
    for i, claim in enumerate(claims):
        if lexical_overlap(claim.text, doc_text) < LEXICAL_OVERLAP_FLOOR:
            _drop_row(i, claim, "prefilter")
        else:
            survivors.append((i, claim))
    if not survivors:
        return [], not_applicable

    # (c) ONE batched request per document; ids index the FULL claim list.
    state: dict = {"doc_id": doc_id}
    questions: dict[str, dict] = {}
    for i, claim in survivors:
        state[f"claim_{i}"] = anchor_window(doc_text, claim.text)
        questions[f"claim_{i}"] = {
            "type": "noul",
            "instructions": "Does this claim follow from the cited document excerpt? Answer noul.",
        }
    decision, latency_ms = _call(decider, state, questions)
    called = bool(decision.applies)
    raw_tokens = int(decision.raw_tokens or 0)

    # Not applicable: fail-closed for LLM content — the deterministic dossier
    # stands alone (no Jev, no unverified claims).
    if not decision.applies:
        for i, claim in survivors:
            _drop_row(i, claim, "no_decider")
        return [], decision

    # Jev error: the whole batch degrades (drop_llm / use_deterministic
    # coincide here — the only fail-open option would be accepting
    # unverified claims, which no config offers).
    if not decision.ok:
        first = True
        for i, claim in survivors:
            ledger.record(
                _row(
                    "citation_soundness", state, decider, run_id,
                    answers=None, deterministic_action="accept",
                    agree=False if enforce else None, agree_direction=None,
                    error="jev_error", latency_ms=latency_ms if first else 0.0,
                    raw_tokens=raw_tokens if first else 0,
                    called=True if first else False, outcome="errored",
                )
            )
            first = False
        logger.debug("G4 batch for {} errored; all LLM claims dropped (run={})", doc_id, run_id)
        return [], decision

    # (d) the floor — one row per judged claim; the batch call's called/
    # raw_tokens/latency ride on the FIRST row so aggregates count once.
    accepted: list[Claim] = []
    first = True
    for i, claim in survivors:
        qid = f"claim_{i}"
        noul = _noul_of(decision.answers, qid)
        if noul is not None and noul >= floor:
            outcome, reason = "accepted", None
        elif noul is None:
            outcome, reason = "dropped", "no_answer"  # malformed/missing verdict: fail-closed
        else:
            outcome, reason = "dropped", "floor"
        # Enforce applies the floor; shadow lets the claim through (the
        # deterministic stages above already bound) and records the would-be.
        if enforce:
            take = outcome == "accepted"
        else:
            take = True
        if take:
            accepted.append(claim)
        agree = noul is not None and outcome == "accepted"
        ledger.record(
            _row(
                "citation_soundness", state, decider, run_id,
                answers={qid: decision.answers.get(qid)},
                deterministic_action="accept", agree=agree,
                agree_direction="match" if agree else None, error=None,
                latency_ms=latency_ms if first else 0.0,
                raw_tokens=raw_tokens if first else 0,
                called=True if first else False, outcome=outcome,
                noul=noul, reason=reason,
            )
        )
        first = False
    return accepted, decision


# --- G5: need promotion ------------------------------------------------------------


def gate_need_promotion(
    need_text: str,
    evidence_excerpt: str,
    deterministic_promote: bool,
    decider,
    gate_cfg: dict | None,
    ledger: DecideLedger,
    run_id: str,
    mode: str,
) -> tuple[bool, float | None, Decision]:
    """G5 — gate promotion of an extracted need (implement -> score boundary).

    noul >= floor promotes; the noul value (the "probability") is stored in
    the row and returned REGARDLESS of the promote/drop outcome. On a Jev
    error (``on_error: drop_llm``) the LLM need is not promoted;
    deterministic needs are unaffected. Not-applicable returns the caller's
    ``deterministic_promote``. Shadow returns the deterministic action and
    records the verdict + agree/agree_direction (``match`` |
    ``jev_yes_det_no`` | ``jev_no_det_yes`` — agreement rate alone conflates
    added strictness with added recall loss).

    Returns (promote, noul, decision).
    """
    cfg = _cfg(gate_cfg)
    floor = _floor(cfg, PROMOTION_FLOOR)
    enforce = mode == "enforce"
    det_action = "promote" if deterministic_promote else "drop"
    state = {
        "need": need_text,
        "evidence_excerpt": anchor_window(evidence_excerpt, need_text),
    }
    questions = {
        "promotion": {
            "type": "noul",
            "instructions": (
                "Does this extracted need represent genuine buying intent? Answer noul."
            ),
        }
    }
    decision, latency_ms = _call(decider, state, questions)
    called = bool(decision.applies)
    raw_tokens = int(decision.raw_tokens or 0)
    on_error = str(cfg.get("on_error", "drop_llm"))

    if not decision.applies:
        ledger.record(
            _row(
                "need_promotion", state, decider, run_id,
                answers=None, deterministic_action=det_action, agree=None,
                agree_direction=None, error=None, latency_ms=latency_ms,
                raw_tokens=raw_tokens, called=False,
            )
        )
        return bool(deterministic_promote), None, decision

    if not decision.ok:
        promote = bool(deterministic_promote) if on_error == "use_deterministic" else False
        ledger.record(
            _row(
                "need_promotion", state, decider, run_id,
                answers=None, deterministic_action=det_action,
                agree=False if enforce else None, agree_direction=None,
                error="jev_error", latency_ms=latency_ms, raw_tokens=raw_tokens,
                called=called, outcome="errored",
            )
        )
        return promote, None, decision

    noul = _noul_of(decision.answers, "promotion")
    would = noul is not None and noul >= floor
    if noul is None:
        agree, direction = None, None
    else:
        agree = would == bool(deterministic_promote)
        if agree:
            direction = "match"
        elif would:
            direction = "jev_yes_det_no"  # Jev adds a promotion det lacks
        else:
            direction = "jev_no_det_yes"  # Jev vetoes = added recall loss
    if enforce:
        promote = would
    else:
        promote = bool(deterministic_promote)  # shadow: deterministic action returned
    # ``outcome`` records the would-be enforce outcome in shadow too — that is
    # the audit signal the floors are tuned from.
    ledger.record(
        _row(
            "need_promotion", state, decider, run_id,
            answers=dict(decision.answers), deterministic_action=det_action,
            agree=agree, agree_direction=direction, error=None,
            latency_ms=latency_ms, raw_tokens=raw_tokens, called=called,
            outcome="accepted" if would else "dropped", noul=noul,
        )
    )
    return promote, noul, decision
