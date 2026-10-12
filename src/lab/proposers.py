"""Multi-family proposer mixer — the autoresearch loop's question source (Task 7).

Per round, each proposer family — the planner model plus the two implementer
models, exactly the three OpenRouter models ``config/decide.yaml`` already
configures — receives the SAME brief (the target definition, the current
questions with their error profiles) and proposes candidate gate questions.
The per-family batches are merged and deduped
(:func:`merge_candidates`) before Task 6's prescreener pays to evaluate them,
so one model's blind spots do not define the question space.

Model ids are READ FROM the decide config, never hardcoded: a family whose
block or model id is missing from ``decide_cfg`` is skipped with a warning —
the lab never spends on a model the config did not name. (The shipped
``config/decide.yaml`` carries xiaomi/mimo-v2.6-pro as the planner and
deepseek/deepseek-v4.1-flash + z-ai/glm-5.3-flash as the implementers.)

TEST SEAM (documented): every family call goes through
``src.llm.implement.call_implementer`` — the same OpenRouter
chat-completions shape and never-raise contract the implementer stage uses
(HTTP errors, timeouts and malformed JSON all return None). This module
imports ``src.llm.implement`` as a MODULE and calls
``implement.call_implementer(...)`` precisely so tests can intercept every
call with ``monkeypatch.setattr("src.llm.implement.call_implementer", fake)``
— fully offline, no network, no tokens.

BUDGET MODEL (documented): estimating tokens_out x model price per family is
overkill for a dev tool — the budget is a simple per-call counter against
``budget_usd`` at a conservative flat :data:`DEFAULT_CALL_COST_USD` ($0.01
per call, charged per ATTEMPT: a failed call may still have spent tokens).
The ceiling is truncated to whole cents before comparison (never
overestimate what a round can afford); a family whose next call would bust
the budget is skipped, and exhaustion mid-round stops the remaining families
honestly (logged) rather than crashing.

NEVER RAISES: :func:`propose_batch` mirrors the implementer seam's contract
— any family failure (provider outage, malformed JSON) logs a warning and
contributes an empty share; an all-families-failed round returns ``[]`` with
a reason on the ``failure`` honesty channel (the ``call_implementer``
``failure`` dict pattern: an optional caller-supplied dict that receives
``failure["reason"]``). Reasons: ``"no_families_configured"``
``"missing_credentials"`` ``"budget_exhausted"`` ``"all_families_failed"``
``"crashed"``.
"""

from __future__ import annotations

import hashlib
import json
import os

from loguru import logger

# The MODULE-level import IS the test seam (see module docstring): call sites
# read ``implement.call_implementer`` off the module attribute, so a test
# double patched onto src.llm.implement.call_implementer intercepts them all.
from src.llm import implement

# The proposer key lives in the untracked .env (never committed, never logged)
# — same env var as the implementers.
API_KEY_ENV = implement.API_KEY_ENV

# Fallback base_url only (mirrors src/llm/planner.py): a family must still
# NAME ITS MODEL in decide_cfg or it is skipped — the model id is the spend
# identity and is never defaulted.
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

# How many candidate questions each family is asked for per round.
CANDIDATES_PER_FAMILY = 5

# Conservative flat cost estimate per family call (see module docstring).
DEFAULT_CALL_COST_USD = 0.01
# The same estimate in whole cents — the budget arithmetic is integer-exact.
_CENTS_PER_CALL = round(DEFAULT_CALL_COST_USD * 100)

_NO_QUESTIONS_LINE = "(none yet — first round)"


# ------------------------------------------------------------------ families


def proposer_families(decide_cfg: dict | None) -> list[dict]:
    """The three proposer families from ``decide_cfg``, in config order.

    Roles: ``planner`` (``decide_cfg["planner"]``), ``bulk`` and ``reasoning``
    (``decide_cfg["implementers"]``) — model ids and base_urls are READ FROM
    the config (a missing base_url falls back to OpenRouter). Only the
    reasoning family carries a ``reasoning_effort`` passthrough when the
    config sets one. A family with no block or no usable model id is SKIPPED
    (logged) — the lab never calls a model the config did not name. Garbage
    inputs (None, non-dict) degrade to fewer families or ``[]``; never raises.
    """
    cfg = decide_cfg if isinstance(decide_cfg, dict) else {}
    impl = cfg.get("implementers")
    impl = impl if isinstance(impl, dict) else {}
    # (role, config block, effort key) — planner first, matching decide.yaml.
    specs = (
        ("planner", cfg.get("planner"), "reasoning_effort"),
        ("bulk", impl.get("bulk"), "reasoning_effort"),
        ("reasoning", impl.get("reasoning"), "reasoning_effort"),
    )
    families: list[dict] = []
    for role, block, effort_key in specs:
        if not isinstance(block, dict):
            logger.warning("proposers: family '{}' absent from decide config -> skipped", role)
            continue
        model = block.get("model")
        if not isinstance(model, str) or not model.strip():
            logger.warning("proposers: family '{}' names no model id -> skipped", role)
            continue
        effort = block.get(effort_key)
        effort = effort.strip() if isinstance(effort, str) and effort.strip() else None
        families.append(
            {
                "role": role,
                "model": model.strip(),
                "base_url": str(block.get("base_url") or DEFAULT_BASE_URL),
                "reasoning_effort": effort,
            }
        )
    return families


# ------------------------------------------------------------------- the brief


def _render(value: object) -> str:
    """Render one brief value as text: strings verbatim, anything else JSON."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def build_brief_messages(
    brief: dict | None, *, n_candidates: int = CANDIDATES_PER_FAMILY
) -> list[dict]:
    """Chat messages for one proposer call: pure, deterministic, secret-free.

    ``brief`` carries ``target`` (the target definition — string or
    structured, rendered as JSON when structured) and ``questions`` (the
    current questions; each entry is a question string or a dict that may
    carry its error profile — rendered whole). Missing/None parts degrade to
    the first-round line; a non-dict brief degrades to an empty one; never
    raises.
    """
    brief = brief if isinstance(brief, dict) else {}
    system = "\n".join(
        [
            "You are a PROPOSER in an autoresearch loop that tunes a decide layer's gate questions.",
            "Candidate questions are screened cheaply, A/B-tested on recorded gate decisions,",
            "and the surviving questions' prediction errors feed the next proposal round.",
            "You widen the search: do NOT restate the current questions; look for angles they miss.",
            "Every question must be one self-contained question answerable from a single staged",
            "document, with a criterion that states what counts as a yes.",
            "",
            'Output STRICT JSON exactly matching: {"questions": [{"text": "...", "criteria": "..."}]}',
            "No prose, no markdown fences, no api_key or secrets in the output.",
        ]
    )
    questions = brief.get("questions")
    if isinstance(questions, list):
        entries: list = questions
    elif questions is None:
        entries = []
    else:  # a bare non-list value still carries information: one entry
        entries = [questions]
    rendered = [f"- {_render(q)}" for q in entries if q is not None]
    question_lines = "\n".join(rendered) if rendered else f"- {_NO_QUESTIONS_LINE}"
    user = "\n".join(
        [
            "Target definition:",
            _render(brief.get("target")) or "(unspecified)",
            "",
            "Current questions and their error profiles:",
            question_lines,
            "",
            f"Propose exactly {n_candidates} NEW candidate questions as strict JSON.",
        ]
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


# -------------------------------------------------------------------- parsing


def parse_candidates(raw: dict | None) -> list[dict]:
    """Atomize a proposer reply ``{"questions": [...]}`` into candidates.

    Tolerant by design (the reply arrives pre-parsed by ``call_implementer``'s
    fence-stripping JSON reader, but a model may still riff on the shape): a
    dict entry yields its ``text`` plus ``criteria`` (extra keys preserved);
    a bare-string entry yields the text with an empty criteria; entries with
    no non-blank text are dropped. Malformed payloads degrade to ``[]``.
    """
    if not isinstance(raw, dict):
        return []
    entries = raw.get("questions")
    if not isinstance(entries, list):
        return []
    out: list[dict] = []
    for entry in entries:
        if isinstance(entry, str):
            entry = {"text": entry}
        if not isinstance(entry, dict):
            continue
        text = entry.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        criteria = entry.get("criteria")
        criteria = criteria.strip() if isinstance(criteria, str) else ""
        cand = dict(entry)
        cand["text"] = text.strip()
        cand["criteria"] = criteria
        out.append(cand)
    return out


# ------------------------------------------------------------------- dedupe


def normalize_text(text: str) -> str:
    """casefold + whitespace collapse (``claim_natural_key``'s normalization)."""
    return " ".join(text.casefold().split())


def criteria_hash(criteria: str) -> str:
    """sha256 hexdigest of the normalized criteria text."""
    return hashlib.sha256(normalize_text(criteria).encode("utf-8")).hexdigest()


def candidate_key(cand: dict) -> tuple[str, str]:
    """The dedupe key: (normalized question text, criteria hash)."""
    text = cand.get("text")
    criteria = cand.get("criteria")
    return (
        normalize_text(text if isinstance(text, str) else ""),
        criteria_hash(criteria if isinstance(criteria, str) else ""),
    )


def merge_candidates(batches: list[list[dict]]) -> list[dict]:
    """Union per-family candidate batches, deduped, stable order.

    The dedupe key is :func:`candidate_key` — normalized question text
    (casefold + whitespace collapse) PLUS the criteria hash, so the same
    question with a materially different criterion survives as a distinct
    candidate while cross-family repeats collapse. First occurrence wins
    (batch order, then in-batch order); non-dict entries and blank texts are
    dropped. Never raises.
    """
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for batch in batches:
        if not isinstance(batch, list):
            continue
        for cand in batch:
            if not isinstance(cand, dict):
                continue
            text = cand.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            key = candidate_key(cand)
            if key in seen:
                continue
            seen.add(key)
            out.append(dict(cand))
    return out


# --------------------------------------------------------------- the batch


def propose_batch(
    decide_cfg: dict | None,
    brief: dict | None,
    *,
    api_key: str | None = None,
    budget_usd: float | None = None,
    failure: dict | None = None,
) -> list[dict]:
    """One proposal round: every family called once, candidates merged.

    Calls :func:`src.llm.implement.call_implementer` once per family (same
    OpenRouter chat-completions shape, same never-raise contract — a failed
    family logs and contributes an empty share). Each returned candidate is
    stamped with ``proposer`` (the family's model id) and ``proposer_role``.

    ``api_key``: the explicit key, else ``OPENROUTER_API_KEY`` from the env
    (the implementers' env var). Missing credentials mean zero calls, not
    errors. ``budget_usd``: the per-round ceiling at the flat
    :data:`DEFAULT_CALL_COST_USD` estimate (see module docstring); ``None``
    means no ceiling. ``failure`` — the honesty channel — receives
    ``failure["reason"]`` when the round yields nothing. NEVER raises.
    """
    try:
        return _propose_batch(
            decide_cfg, brief, api_key=api_key, budget_usd=budget_usd, failure=failure
        )
    except Exception as exc:  # the belt in never-raise: nothing escapes
        logger.error("propose_batch crashed (never-raise): {}: {}", type(exc).__name__, exc)
        if failure is not None:
            failure["reason"] = "crashed"
        return []


def _propose_batch(
    decide_cfg: dict | None,
    brief: dict | None,
    *,
    api_key: str | None,
    budget_usd: float | None,
    failure: dict | None,
) -> list[dict]:
    families = proposer_families(decide_cfg)
    if not families:
        logger.warning("propose_batch: no proposer families in decide config")
        if failure is not None:
            failure["reason"] = "no_families_configured"
        return []

    key = api_key or os.environ.get(API_KEY_ENV)
    if not key:
        logger.debug("propose_batch: missing {} -> no proposals", API_KEY_ENV)
        if failure is not None:
            failure["reason"] = "missing_credentials"
        return []

    # Data-exit consent — the same string that gates every other lab egress
    # seam (backfill, the runner's decider) and the shipped planner. The
    # brief carries target/question text (not document bodies), but the
    # posture is uniform: no consent, no outbound call (review finding).
    from src.decide.jev import DATA_EXIT_CONSENT

    if os.environ.get("SIGNALS_DECIDE_DATA_EXIT") != DATA_EXIT_CONSENT:
        logger.debug("propose_batch: data-exit consent absent -> no proposals")
        if failure is not None:
            failure["reason"] = "missing_consent"
        return []

    # Integer-cent budget arithmetic (see module docstring): a family is
    # called only when its call fits the truncated ceiling.
    budget_cents: int | None = None
    if isinstance(budget_usd, (int, float)) and not isinstance(budget_usd, bool):
        budget_cents = int(budget_usd * 100)  # truncation = conservative

    messages = build_brief_messages(brief)
    out: list[dict] = []
    calls_made = 0
    for family in families:
        if budget_cents is not None and (calls_made + 1) * _CENTS_PER_CALL > budget_cents:
            logger.info(
                "propose_batch: budget exhausted before family '{}' ({} of {} calls made)",
                family["role"],
                calls_made,
                len(families),
            )
            continue
        raw = implement.call_implementer(
            messages,
            model=family["model"],
            base_url=family["base_url"],
            api_key=key,
            reasoning_effort=family.get("reasoning_effort"),
        )
        calls_made += 1  # charged per ATTEMPT — a failed call may have spent
        if raw is None:
            logger.warning(
                "propose_batch: family '{}' ({}) failed -> empty share",
                family["role"],
                family["model"],
            )
            continue
        for cand in parse_candidates(raw):
            cand["proposer"] = family["model"]
            cand["proposer_role"] = family["role"]
            out.append(cand)

    if not out and failure is not None:
        if calls_made == 0 and budget_cents is not None and _CENTS_PER_CALL > budget_cents:
            failure["reason"] = "budget_exhausted"
        else:
            failure["reason"] = "all_families_failed"
    return out
