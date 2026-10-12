"""Question prescreener + correlation pruning — the round's admission gate (Task 6).

Never pay to A/B a bad question. Before a candidate gate question enters a
replay round, :func:`prescreen` turns the question itself into the Jev state
and asks the cookbook battery's four ZERO-POLICY Nouls (no criteria on the
questions, no floor policy inside the model — the floors live here), adapted
to this repo's row = staged doc:

- ``answerable_from_source`` — decidable from a single document's text alone
- ``one_meaning_under_criteria`` — unambiguous given the question's criteria
- ``applies_to_most_rows`` — meaningful for most staged documents
- ``varies_across_rows`` — the answer actually DIFFERS across documents

All four must clear their floors (a config dict, default 0.60 each, partial
overrides allowed) in ONE batched request per candidate whose state carries
the question, its criteria, and a deterministic :func:`sample_digest` of the
stratified sample (30 docs) the judgment was evaluated against.

DECIDER CONTRACT (src/decide/jev.py + gates.py ``_call``): the shipped
deciders accept the optional third ``decide(state, questions, gate=...)``
parameter — the gate threads to the breaker's exclusion math — while
two-argument deciders (every test fake) are called WITHOUT it; the same
per-type introspection gates.py ships decides which shape applies. Never
raises past a candidate: the seam's own never-raise contract already
collapses Jev failures into ``ok=False`` Decisions, and this module adds the
belt (a crashing decider fails THAT candidate honestly with the exception's
cause — ``Decision.cause`` flows onto the row — and screening continues).

:func:`prune_correlated` then drops near-duplicate questions BEFORE any
encoding budget is spent downstream: over the dev corpus's per-question noul
columns, pairwise |Pearson r| strictly above ``threshold`` (default 0.85)
links questions into clusters (simple connected components), and each cluster
keeps its clearest member — the higher screening score (the sum of the four
screening Nouls), ties broken by the SHORTER prompt (cheaper), then by input
order (deterministic). Pure function, stdlib only (numpy buys nothing here:
the pairwise loop is over dozens of questions, not thousands of columns).

Stdlib + src.decide only, no pipeline imports (the scope guard: src/lab
never enters the intel import graph).
"""

from __future__ import annotations

import inspect
import json
import math
import time

from loguru import logger

from src.decide.gates import state_hash
from src.decide.shapes import Decision, exception_cause

# The ledger name this screening would carry (mirrors the gates' boundary tags).
GATE = "lab_prescreen"

# The cookbook battery, adapted to row = staged doc. Order is the row/report order.
SCREENING_NOULS = (
    "answerable_from_source",
    "one_meaning_under_criteria",
    "applies_to_most_rows",
    "varies_across_rows",
)

# Every noul clears at 0.60 unless the floors config dict says otherwise.
DEFAULT_FLOOR = 0.60

# Row outcomes (the honesty vocabulary: why a candidate did or did not survive).
OUTCOME_PASSED = "passed"
OUTCOME_FAILED_FLOOR = "failed_floor"  # the decision was fine; a noul missed its floor
OUTCOME_FAILED_DECISION = "failed_decision"  # the decider failed / never applied
OUTCOME_SKIPPED = "skipped"  # no usable question text: no request was paid

# Per-state preview cap inside the sample digest (chars) — the digest is
# evidence for applies_to_most_rows/varies_across_rows, not a document dump.
PREVIEW_CHARS = 200

# Pseudo-typed causes for failure modes that carry no Decision.cause.
_CAUSE_NOT_APPLICABLE = "NotApplicable: decider skipped the screening call"
_CAUSE_JEV_ERROR = "jev_error"  # the ledger write sites' legacy fallback

# Correlation-prune default: |Pearson r| strictly ABOVE this links a pair.
DEFAULT_PRUNE_THRESHOLD = 0.85


# --------------------------------------------------------------------- digest


def _preview(state: object) -> str:
    """One bounded preview of a sample state: strings verbatim, anything
    else canonical JSON (the state_hash recipe), truncated to PREVIEW_CHARS."""
    text = state if isinstance(state, str) else _canonical(state)
    return text[:PREVIEW_CHARS]


def _canonical(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def sample_digest(sample_states: list) -> dict:
    """Deterministic digest of the stratified sample: ``n``, a sha256
    ``fingerprint`` over the canonical states (identifies the exact sample on
    every row — re-runs can spot identical inputs, state_hash's purpose), and
    bounded ``previews`` (the model-facing evidence for the applies/varies
    Nouls). Pure: same sample in, same digest out, always."""
    states = list(sample_states or [])
    return {
        "n": len(states),
        "fingerprint": state_hash(states),
        "previews": [_preview(s) for s in states],
    }


# ------------------------------------------------------------ decider plumbing


def _noul_questions() -> dict:
    """The four zero-policy screening questions — fresh dicts per call (no
    shared mutation), each noul-typed with instructions only (the floors are
    enforced HERE, never inside the model)."""
    return {
        "answerable_from_source": {
            "type": "noul",
            "instructions": (
                "Could this question be answered entirely from the text of a "
                "single staged document, without outside knowledge? Answer noul."
            ),
        },
        "one_meaning_under_criteria": {
            "type": "noul",
            "instructions": (
                "Given the stated criteria, does this question have exactly "
                "one interpretation? Answer noul."
            ),
        },
        "applies_to_most_rows": {
            "type": "noul",
            "instructions": (
                "Would this question yield a meaningful answer for most of the "
                "sampled staged documents? Answer noul."
            ),
        },
        "varies_across_rows": {
            "type": "noul",
            "instructions": (
                "Would this question's answer genuinely differ across the "
                "sampled staged documents? Answer noul."
            ),
        },
    }


def _decides_with_gate(decider) -> bool:
    """Does this decider's ``decide()`` accept ``gate=``? (gates.py's rule,
    mirrored here so the lab does not reach into the gates' privates: a
    ``gate`` parameter or **kwargs accepts it, anything else does not.)"""
    try:
        params = inspect.signature(decider.decide).parameters
    except (TypeError, ValueError):  # exotic callables: assume the old shape
        return False
    return any(
        name == "gate" or param.kind is inspect.Parameter.VAR_KEYWORD
        for name, param in params.items()
    )


def _call(decider, state, questions) -> tuple[Decision, float]:
    """One timed decider invocation, gate threaded when the decider accepts it
    (gates.py ``_call``'s shape; the gate is fixed here, not a parameter)."""
    t0 = time.perf_counter()
    if _decides_with_gate(decider):
        decision = decider.decide(state, questions, gate=GATE)
    else:
        decision = decider.decide(state, questions)
    latency_ms = round((time.perf_counter() - t0) * 1000.0, 3)
    return decision, latency_ms


def _noul_of(answers, qid: str) -> float | None:
    """The numeric noul under ``qid`` (gates.py ``_noul_of`` mirrored) —
    None for a missing, non-numeric, boolean, or NaN answer."""
    ans = (answers or {}).get(qid)
    if isinstance(ans, dict):
        value = ans.get("noul")
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and not math.isnan(value)
        ):
            return float(value)
    return None


# ------------------------------------------------------------------- prescreen


def _effective_floors(floors: dict | None) -> dict:
    """Per-noul floors: DEFAULT_FLOOR each, overridden only where the config
    dict names a noul (partial overrides are the point). A garbage value
    falls back to the default with a warning — never a crash."""
    cfg = floors if isinstance(floors, dict) else {}
    out: dict = {}
    for name in SCREENING_NOULS:
        raw = cfg.get(name, DEFAULT_FLOOR)
        try:
            out[name] = float(raw)
        except (TypeError, ValueError):
            logger.warning(
                "prescreen: bad floor {!r} for '{}' ({}); using default {}",
                raw, name, floors, DEFAULT_FLOOR,
            )
            out[name] = DEFAULT_FLOOR
    return out


def _candidate_parts(cand: object) -> tuple[str | None, str]:
    """(question text, criteria) from a candidate — the proposers' dict shape
    ({"text", "criteria"}), a bare string, or a dict keyed "question"."""
    if isinstance(cand, str):
        return cand, ""
    if isinstance(cand, dict):
        text = cand.get("text")
        if not isinstance(text, str) or not text.strip():
            text = cand.get("question")
        criteria = cand.get("criteria")
        return (text if isinstance(text, str) else None,
                criteria if isinstance(criteria, str) else "")
    return None, ""


def _row(
    index: int,
    text: str,
    criteria: str,
    *,
    passed: bool,
    outcome: str,
    floors: dict,
    nouls: dict | None = None,
    failing: list | None = None,
    missing: list | None = None,
    cause: str | None = None,
    raw_tokens: int = 0,
    latency_ms: float = 0.0,
    model: str | None,
    sample_fingerprint: str,
    run_id: str,
) -> dict:
    """One honest screening row (the shape the lab's round files consume)."""
    return {
        "gate": GATE,
        "run_id": run_id,
        "index": index,
        "question": text,
        "criteria": criteria,
        "passed": passed,
        "outcome": outcome,
        "nouls": {n: (nouls or {}).get(n) for n in SCREENING_NOULS},
        "floors": dict(floors),
        "failing": list(failing or []),
        "missing": list(missing or []),
        "cause": cause,
        "raw_tokens": int(raw_tokens or 0),
        "latency_ms": latency_ms,
        "model": model,
        "sample_fingerprint": sample_fingerprint,
    }


def prescreen(
    candidates: list,
    sample_states: list,
    decider,
    *,
    floors: dict | None = None,
    run_id: str = "lab",
) -> tuple[list, list]:
    """Screen candidate gate questions against the four-Noul battery.

    ONE batched decider request per candidate: the state is the question
    itself (``{"question", "criteria", "sample_digest"}``), the questions are
    the four zero-policy Nouls, and the decider is called under the repo's
    seam contract with ``gate="lab_prescreen"`` threaded where accepted.
    Returns ``(survivors, rows)``:

    - ``survivors`` — copies of the passing candidates under the canonical
      ``question`` key, plus their screening ``nouls``, the summed
      ``screening_score``, and the ``sample_fingerprint`` they cleared
      (everything :func:`prune_correlated` needs to rank a cluster).
    - ``rows`` — one honest row per candidate (passed / failed_floor with the
      failing Nouls named / failed_decision with the decision's ``cause`` /
      skipped for blank texts, which pay no request).

    NEVER raises past a candidate: a failed or crashing decision fails that
    candidate honestly and screening continues to the next one.
    """
    eff_floors = _effective_floors(floors)
    digest = sample_digest(sample_states)
    model = getattr(decider, "model", None)

    survivors: list = []
    rows: list = []
    for i, cand in enumerate(candidates or []):
        text, criteria = _candidate_parts(cand)
        if text is None or not text.strip():
            rows.append(
                _row(
                    i, text or "", criteria,
                    passed=False, outcome=OUTCOME_SKIPPED, floors=eff_floors,
                    model=model, sample_fingerprint=digest["fingerprint"],
                    run_id=run_id,
                )
            )
            continue

        state = {
            "question": text,
            "criteria": criteria,
            "sample_digest": digest,
        }
        try:
            decision, latency_ms = _call(decider, state, _noul_questions())
        except Exception as exc:  # the belt in never-raise: nothing escapes
            logger.warning(
                "prescreen: decider crashed on candidate {!r}: {}", text, exception_cause(exc)
            )
            rows.append(
                _row(
                    i, text, criteria,
                    passed=False, outcome=OUTCOME_FAILED_DECISION, floors=eff_floors,
                    cause=exception_cause(exc), latency_ms=0.0,
                    model=model, sample_fingerprint=digest["fingerprint"],
                    run_id=run_id,
                )
            )
            continue

        raw_tokens = int(decision.raw_tokens or 0)

        if not decision.applies or not decision.ok:
            # Layer off / Jev failure: the candidate fails screening honestly,
            # the row naming the decision's cause when it carried one.
            cause = decision.cause or (
                _CAUSE_NOT_APPLICABLE if not decision.applies else _CAUSE_JEV_ERROR
            )
            rows.append(
                _row(
                    i, text, criteria,
                    passed=False, outcome=OUTCOME_FAILED_DECISION, floors=eff_floors,
                    cause=cause, raw_tokens=raw_tokens, latency_ms=latency_ms,
                    model=model, sample_fingerprint=digest["fingerprint"],
                    run_id=run_id,
                )
            )
            continue

        nouls = {n: _noul_of(decision.answers, n) for n in SCREENING_NOULS}
        missing = [n for n in SCREENING_NOULS if nouls[n] is None]
        # A noul clears only with a numeric answer at or above its floor; a
        # missing answer can never clear (an unanswered question is not a pass).
        failing = [n for n in SCREENING_NOULS if nouls[n] is None or nouls[n] < eff_floors[n]]

        if failing:
            rows.append(
                _row(
                    i, text, criteria,
                    passed=False, outcome=OUTCOME_FAILED_FLOOR, floors=eff_floors,
                    nouls=nouls, failing=failing, missing=missing,
                    raw_tokens=raw_tokens, latency_ms=latency_ms,
                    model=model, sample_fingerprint=digest["fingerprint"],
                    run_id=run_id,
                )
            )
            continue

        score = sum(nouls.values())
        survivor = dict(cand) if isinstance(cand, dict) else {}
        survivor["question"] = text
        survivor["criteria"] = criteria
        survivor["nouls"] = dict(nouls)
        survivor["screening_score"] = score
        survivor["sample_fingerprint"] = digest["fingerprint"]
        survivors.append(survivor)
        rows.append(
            _row(
                i, text, criteria,
                passed=True, outcome=OUTCOME_PASSED, floors=eff_floors,
                nouls=nouls, raw_tokens=raw_tokens, latency_ms=latency_ms,
                model=model, sample_fingerprint=digest["fingerprint"],
                run_id=run_id,
            )
        )

    return survivors, rows


# ------------------------------------------------------------------- prune


def _column(scores: object) -> list:
    """Coerce one question's per-row noul column to float-or-None (a
    non-numeric entry reads as not answered on that row)."""
    if not isinstance(scores, (list, tuple)):
        return []
    out: list = []
    for v in scores:
        if (
            isinstance(v, (int, float))
            and not isinstance(v, bool)
            and not math.isnan(v)
        ):
            out.append(float(v))
        else:
            out.append(None)
    return out


def _pearson(xs: list, ys: list) -> float | None:
    """Pearson r over the rows where BOTH columns answered — None when
    undefined (fewer than 2 overlapping rows, or either side constant:
    zero variance has no correlation to prune on)."""
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    n = len(pairs)
    if n < 2:
        return None
    mx = sum(x for x, _ in pairs) / n
    my = sum(y for _, y in pairs) / n
    dx = [x - mx for x, _ in pairs]
    dy = [y - my for _, y in pairs]
    var_x = sum(d * d for d in dx)
    var_y = sum(d * d for d in dy)
    if var_x <= 0.0 or var_y <= 0.0:
        return None
    return sum(a * b for a, b in zip(dx, dy)) / math.sqrt(var_x * var_y)


def _screening_score(result: dict) -> float:
    """The cluster-ranking score: the SUM OF THE FOUR screening Nouls (the
    spec's definition), falling back to a carried ``screening_score`` when
    the nouls are absent, else 0.0 — never raises on a thin dict."""
    nouls = result.get("nouls")
    if isinstance(nouls, dict):
        values = [
            float(v)
            for v in nouls.values()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        ]
        if values:
            return sum(values)
    explicit = result.get("screening_score")
    if isinstance(explicit, (int, float)) and not isinstance(explicit, bool):
        return float(explicit)
    return 0.0


def _prompt_len(result: dict) -> int:
    """The cheapness tiebreak: the prompt the decider would be sent — the
    question text plus its criteria."""
    question = result.get("question") or result.get("text")
    criteria = result.get("criteria")
    return len(question if isinstance(question, str) else "") + len(
        criteria if isinstance(criteria, str) else ""
    )


def prune_correlated(question_results: list, *, threshold: float = DEFAULT_PRUNE_THRESHOLD) -> list:
    """Drop near-duplicate questions before any downstream encoding spend.

    ``question_results`` — one dict per surviving question: its ``question``
    text, ``criteria``, its screening ``nouls`` (or the summed
    ``screening_score``), and ``scores`` — the per-corpus-row noul column
    (float per row, None where the question was not answered on that row).

    Pairwise |Pearson r| computed over the rows where both answered; a pair
    strictly above ``threshold`` (default 0.85) links into clusters via
    simple connected components (transitivity is the point: A~B, B~C prunes
    A and C even when A vs C alone would not trip). Each cluster keeps its
    clearest member — the higher screening score, then the shorter prompt
    (cheaper), then the earlier input position. Pure: input order preserved,
    inputs never mutated, singletons always kept.
    """
    results = [r for r in (question_results or []) if isinstance(r, dict)]
    n = len(results)
    if n <= 1:
        return list(results)

    columns = [_column(r.get("scores")) for r in results]

    # Union-find over the above-threshold pairs.
    parent = list(range(n))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]  # path halving
            a = parent[a]
        return a

    for i in range(n):
        for j in range(i + 1, n):
            r = _pearson(columns[i], columns[j])
            if r is not None and abs(r) > threshold:
                root_i, root_j = find(i), find(j)
                if root_i != root_j:
                    parent[root_j] = root_i

    clusters: dict[int, list[int]] = {}
    for idx in range(n):
        clusters.setdefault(find(idx), []).append(idx)

    keepers: set[int] = set()
    for members in clusters.values():
        best = min(
            members,
            key=lambda idx: (-_screening_score(results[idx]), _prompt_len(results[idx]), idx),
        )
        keepers.add(best)

    return [r for idx, r in enumerate(results) if idx in keepers]
