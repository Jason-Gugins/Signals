"""Consistency sampler: repeat-and-measure calibration audits over a Decider.

The calibration method behind the decide-layer bands: run the SAME state and
rubric through the decider ``samples`` times and measure how much the
verdicts wobble. Per the consistency cookbooks, the repeat-and-measure loop
runs over REAL traffic (a sampled slice of the day's dossier states) to tune
the band edges empirically. CAVEAT carried from the cookbooks: the band
edges published in config are NOT calibrated numbers — real edges come from
OUR labeled cases plus the project's error/review costs, and this sampler is
the measurement tool for that work, not a substitute for it.

Each call gets a DEEP COPY of the state with a fresh throwaway ``uid``
(uuid4 hex) under ``uid_key`` — the "uid buster" that defeats any
decider-side memoization keyed on state identity, forcing independent
draws. The caller's state object is NEVER mutated: a dict state is deep-
copied then stamped; a str state gets a uid annotation line appended
(``"<state>\\n\\n[<uid_key>: <hex>]"`` — chosen over wrapping because the
decider sees the original text verbatim, only longer, and the sampler never
has to unwrap anything).

Never raises past the seam (the jev.py convention): a decision with
``ok=False`` makes that whole call's answers unusable, so EVERY question
collects a NaN sample for that call and ``parse_failures`` counts each one;
an exception from the decider is impossible per the protocol but is caught
broadly anyway and treated the same. Per-question metrics (``std``/``mean``
/``min``/``max``/``n_valid``) are computed EXCLUDING the NaN samples.
``std`` is ``statistics.stdev`` (the n-1 sample estimator) when at least
two valid samples exist, else 0.0 — a single observation carries no spread
information and the audit bands floor at zero spread.
"""

from __future__ import annotations

import copy
import math
import statistics
import uuid
from typing import Protocol, runtime_checkable

__all__ = ["sample_consistency"]

DEFAULT_SAMPLES = 15
DEFAULT_UID_KEY = "uid"


@runtime_checkable
class _DeciderLike(Protocol):
    """Narrow structural view of the Decider seam (avoids a jev import cycle)."""

    def decide(self, state: str | dict, questions: dict[str, dict]) -> object: ...


def _stamped_state(state: str | dict, uid_key: str, uid: str) -> str | dict:
    """Return a fresh per-call state carrying ``uid`` — never mutates input."""
    if isinstance(state, dict):
        stamped = copy.deepcopy(state)
        stamped[uid_key] = uid
        return stamped
    # str state: append a uid annotation line (see module docstring).
    return f"{state}\n\n[{uid_key}: {uid}]"


def _read_answer(answer: object, qtype: str) -> tuple[float | None, str | None]:
    """Pull the measured value (and choice label, if any) from one answer.

    Returns ``(value, label)``; ``value is None`` means missing/malformed ->
    NaN + parse failure. For ``choice`` a concrete label with a numeric
    probability for it is required — the label's probability is the measured
    quantity, so either half missing fails the sample.
    """
    if not isinstance(answer, dict):
        return None, None
    if qtype == "choice":
        label = answer.get("choice")
        if not isinstance(label, str) or not label:
            return None, None
        probs = answer.get("probabilities")
        if not isinstance(probs, dict):
            return None, None
        prob = probs.get(label)
        if not isinstance(prob, (int, float)) or isinstance(prob, bool):
            return None, None
        return float(prob), label
    # noul (and any score-shaped fallback): the bare numeric field.
    value = answer.get(qtype if qtype in ("noul", "score") else "noul")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None, None
    return float(value), None


def sample_consistency(
    decider: _DeciderLike,
    state: str | dict,
    questions: dict[str, dict],
    samples: int = DEFAULT_SAMPLES,
    uid_key: str = DEFAULT_UID_KEY,
) -> dict:
    """Repeat-and-measure one state/rubric pair; return per-question spread.

    ``samples`` calls to ``decider.decide``, each with a deep-copied state
    carrying a fresh throwaway uid (see module docstring for the calibration
    purpose and the not-calibrated-bands caveat). Question ids are the keys
    of ``questions``; ``type`` is ``"noul"`` (bare float measured) or
    ``"choice"`` (the CHOSEN label's probability measured, plus the label
    histogram). Missing/malformed answers, and any whole call with
    ``ok=False`` (or a broadly-caught exception), become NaN samples counted
    in ``parse_failures``.

    Returns::

        {
            "samples": <calls actually made>,
            "questions": {qid: {
                "std": float, "mean": float, "min": float, "max": float,
                "n_valid": int,                      # non-NaN samples
                # choice only:
                "labels": {label: count},
                "conflicts": 1 if >1 concrete label across samples else 0,
            }},
            "parse_failures": int,
        }

    With zero valid samples, ``mean``/``min``/``max`` are NaN and ``std`` is
    0.0. Uids are deliberately NOT returned (noise in the audit record).
    """
    values: dict[str, list[float]] = {qid: [] for qid in questions}
    labels: dict[str, list[str | None]] = {qid: [] for qid in questions}
    parse_failures = 0
    samples_run = 0

    for _ in range(samples):
        samples_run += 1
        uid = uuid.uuid4().hex
        call_state = _stamped_state(state, uid_key, uid)
        try:
            decision = decider.decide(call_state, questions)
        except Exception:  # impossible per the protocol; never propagate
            decision = None
        if decision is None or not getattr(decision, "ok", False):
            # Unusable call: a NaN sample (and a parse failure) per question.
            parse_failures += len(questions)
            for qid in questions:
                values[qid].append(math.nan)
                if questions[qid].get("type") == "choice":
                    labels[qid].append(None)
            continue
        answers = getattr(decision, "answers", None) or {}
        for qid, spec in questions.items():
            qtype = spec.get("type", "noul")
            value, label = _read_answer(answers.get(qid), qtype)
            if value is None:
                parse_failures += 1
                value = math.nan
            values[qid].append(value)
            if qtype == "choice":
                labels[qid].append(label)

    questions_out: dict[str, dict] = {}
    for qid, spec in questions.items():
        valid = [v for v in values[qid] if not math.isnan(v)]
        n_valid = len(valid)
        if n_valid >= 1:
            mean = statistics.fmean(valid)
            vmin, vmax = min(valid), max(valid)
        else:
            mean = math.nan
            vmin = vmax = math.nan
        entry: dict = {
            "std": statistics.stdev(valid) if n_valid >= 2 else 0.0,
            "mean": mean,
            "min": vmin,
            "max": vmax,
            "n_valid": n_valid,
        }
        if spec.get("type") == "choice":
            counts: dict[str, int] = {}
            for label in labels[qid]:
                if label is not None:
                    counts[label] = counts.get(label, 0) + 1
            entry["labels"] = counts
            entry["conflicts"] = 1 if len(counts) > 1 else 0
        questions_out[qid] = entry

    return {
        "samples": samples_run,
        "questions": questions_out,
        "parse_failures": parse_failures,
    }
