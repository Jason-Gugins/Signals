"""Implementer sub-stage: per-document claim extraction + the five fields.

The implementers are an ORCHESTRATOR-LEVEL pass that runs AFTER the
deterministic derive collect pass and reads ALREADY-STORED documents
(RawStore is doc_id-keyed). Their output is ADDITIVE to the deterministic
derive output, which is always produced in full: any failure below drops
the LLM content for that boundary and leaves exactly today's dossier.

SERIAL in v1 — no per-document parallelism. :func:`implement_batch` is the
shared serial contract (plain loop, no threads, no asyncio) so G3/G4 and
the cumulative token counter stay on the calling thread; the runner's
threading wraps only ``_fetch_one``, so stage-boundary calls need no
locking.

No code from this module runs inside the needs source or inside any
``parse()``: the "no LLM" guarantees stated in ``src/intel/market.py``
("no stemming, no fuzzy matching, no embeddings, no LLM, no caching") and
``src/sources/needs/extract.py`` ("no stemming, no fuzzy matching, and no
LLM") remain literally true.

Models (config/decide.yaml ``implementers``): the bulk role
(``deepseek/deepseek-v4.1-flash``) extracts atomized claims per document;
the reasoning role (``z-ai/glm-5.3-flash``, ``max`` effort) fills the five
``prompt.md`` dossier fields. The reasoning parameter is keyed on the
base_url with ONE conditional: OpenRouter takes the unified nested
``{"reasoning": {"effort": ...}}`` (its standalone ``reasoning_effort``
does not accept ``max``), native endpoints take ``reasoning_effort``.

Failure seam (same discipline as ``src/decide/jev.py`` and
``src/llm/planner.py``): :func:`call_implementer` and
:func:`extract_five_fields` NEVER raise — HTTP errors, timeouts and
malformed JSON all come back as ``None`` so a provider outage degrades to
the deterministic pipeline. Keys are never logged: no log line carries the
Authorization header or the api_key.

Gating: :func:`screen_and_gate_claims` is the G4 path — a deterministic
doc_id screen runs BEFORE any Jev spend (a claim citing a different
document than the batch's is a hallucinated citation), then the shared
``gate_citation_batch`` (doc existence, lexical-overlap prefilter, the
deterministic quote-span check, ONE batched Jev request, floor) does the
gating. The gate asks one 3-way Choice question per claim (supports /
contradicted / says_nothing) and :func:`screen_and_gate_claims` harvests
each returned claim's verdict meta from the batch answers. Claims become
``SignalCandidate`` objects (:func:`claims_to_candidates`) that flow
through ``normalize_batch`` like every other candidate — which is why the
``llm_need`` type is registered under ``types:`` in ``config/signals.yaml``.
G5 (need promotion) gates those candidates' PROMOTION at the
implement -> score boundary in the orchestrator wiring, not here. The five
dossier fields are G4-gated too (``five_fields_to_claims`` feeds the same
citation path) but are dossier CONTENT, not signals — G5 does not apply
to them; they are marked ``llm_authored`` with the model id downstream.
"""

from __future__ import annotations

import hashlib
import json

import httpx
from loguru import logger

from src.core.textutil import truncate
from src.decide.gates import CLAIM_FLOOR, anchor_window, gate_citation_batch, state_hash
from src.decide.shapes import Claim
from src.signals.normalize import SignalCandidate

# The implementer key lives in the untracked .env (never committed, never logged).
API_KEY_ENV = "OPENROUTER_API_KEY"

DEFAULT_TIMEOUT_S = 60.0
# The reasoning implementer thinks longer: a higher default per-call timeout.
DEFAULT_REASONING_TIMEOUT_S = 90.0
DEFAULT_REASONING_EFFORT = "max"

# Extraction caps (documented module constants): quick route shortens the
# instructions and caps the claim count; deep allows the full batch.
MAX_CLAIMS_QUICK = 5
MAX_CLAIMS_DEEP = 20

# The five prompt.md dossier fields (intel_package.render_prompt contract).
FIVE_FIELDS: tuple[str, ...] = (
    "operational_need",
    "buying_window",
    "displacement_risk",
    "expansion_signal",
    "why_now",
)

# Confidence used when no verified Choice probability was harvested
# (documented default; normalize_candidate clamps 0..1 anyway).
NO_NOUL_CONFIDENCE = 0.5
# Signal type registered in config/signals.yaml (mirrors need_statement).
DEFAULT_SIGNAL_TYPE = "llm_need"
# The gate whose verdict every candidate here has passed.
CITATION_GATE = "citation_soundness"
# Title length cap for candidates (~80 chars, house truncate habit).
TITLE_CHARS = 80


# ------------------------------------------------------------------- prompts


def build_extraction_messages(
    doc_id: str,
    doc_text: str,
    doc_meta: dict | None = None,
    *,
    route: str = "deep",
) -> list[dict]:
    """Chat messages for one document's claim extraction (bulk implementer).

    The system prompt mirrors the ``render_prompt`` citation contract
    (src/export/intel_package.py): use ONLY the supplied document, every
    claim carries the document's doc_id, and every claim carries a
    ``"quote"`` — a verbatim span (<=300 characters, copied UNCHANGED from
    the document, may span a line break) that directly supports it — so the
    gate's deterministic quote-span check can verify grounding.
    ``route="quick"`` shortens the instructions and caps extraction at
    :data:`MAX_CLAIMS_QUICK`; ``route="deep"`` (the default; any unknown
    route is treated as deep) allows :data:`MAX_CLAIMS_DEEP`. Pure and
    deterministic — no network, no model call.
    """
    quote_line = (
        'Every claim must include a "quote": a verbatim span of at most 300 characters\n'
        "copied UNCHANGED from the document (it may span a line break) that directly\n"
        "supports the claim."
    )
    if route == "quick":
        cap_line = (
            f"Extract AT MOST {MAX_CLAIMS_QUICK} claims: only the strongest "
            "need / intent statements."
        )
        contract = (
            "Use ONLY the supplied document. Every claim must carry the document's doc_id.\n"
            f"{quote_line}\n"
            "If the document does not support a claim, do not emit it."
        )
    else:
        cap_line = (
            f"Extract AT MOST {MAX_CLAIMS_DEEP} atomized claims: one self-contained\n"
            "fact per claim, each a statement the document itself supports."
        )
        contract = (
            "Use ONLY the supplied document. Every claim must carry the document's doc_id.\n"
            f"{quote_line}\n"
            "If the document does not support a claim, do not emit it. Do not use any\n"
            "outside knowledge and do not invent facts that are not present in the document."
        )
    system = "\n".join(
        [
            "You are the IMPLEMENTER for one stored document in a signal-collection run.",
            "",
            contract,
            "",
            cap_line,
            "",
            'Output STRICT JSON exactly matching: {"claims": [{"text": "...", '
            '"doc_id": "...", "quote": "..."}]}',
            "No prose, no markdown fences, no api_key or secrets in the output.",
        ]
    )
    meta_lines = [f"{key}: {value}" for key, value in sorted((doc_meta or {}).items())]
    user = "\n".join(
        [f"Document doc_id: {doc_id}", *meta_lines, "", "Document text:", doc_text]
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _build_five_fields_messages(doc_id: str, doc_text: str) -> list[dict]:
    """Chat messages for the five dossier fields (reasoning implementer).

    Mirrors the ``render_prompt`` field list and its "answer unknown rather
    than guessing" fallback; the output shape is one object per field.
    """
    system = "\n".join(
        [
            "You are the REASONING IMPLEMENTER for one stored document in a signal-collection run.",
            "",
            "Use ONLY the supplied document. Every field must cite the document's doc_id.",
            'If the document does not support a field, answer "unknown" for that field rather',
            "than guessing. Do not use any outside knowledge and do not invent facts that are",
            "not present in the document.",
            "",
            "Fill these fields:",
            *[f"- {field}" for field in FIVE_FIELDS],
            "",
            'Output STRICT JSON exactly matching: {"<field>": {"text": "...", "doc_id": "..."}}',
            "for each of the five fields. A field with no supporting evidence must be",
            'the bare string "unknown" or {"text": "unknown"}.',
            "No prose, no markdown fences, no api_key or secrets in the output.",
        ]
    )
    user = "\n".join([f"Document doc_id: {doc_id}", "", "Document text:", doc_text])
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


# --------------------------------------------------------------------- client


def _parse_content(content: object) -> dict | None:
    """Parse the LLM message content as JSON, stripping ``` fences if present."""
    if not isinstance(content, str):
        return None
    text = content.strip()
    if text.startswith("```"):
        text = text[3:]
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
        end = text.rfind("```")
        if end != -1:
            text = text[:end]
        text = text.strip()
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def call_implementer(
    messages: list[dict],
    *,
    model: str,
    base_url: str,
    api_key: str,
    reasoning_effort: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict | None:
    """One POST ``{base_url}/chat/completions``; parsed JSON dict or None.

    Body: ``{"model", "messages", "temperature": 0}`` plus, only when
    ``reasoning_effort`` is not None, the reasoning parameter — ONE
    conditional keyed on the base_url: an OpenRouter base_url takes the
    unified ``{"reasoning": {"effort": ...}}``; any other base_url takes the
    native ``"reasoning_effort"``.

    NEVER raises: HTTP errors, timeouts, transport failures, non-JSON bodies
    and malformed content all return None (logged at warning without the
    key). The api_key goes into the Authorization header only and is never
    logged.
    """
    url = f"{base_url.rstrip('/')}/chat/completions"
    body: dict = {"model": model, "messages": messages, "temperature": 0}
    if reasoning_effort is not None:
        if "openrouter" in base_url:
            body["reasoning"] = {"effort": reasoning_effort}
        else:
            body["reasoning_effort"] = reasoning_effort
    try:
        with httpx.Client(timeout=timeout_s) as client:
            resp = client.post(url, json=body, headers={"Authorization": f"Bearer {api_key}"})
            resp.raise_for_status()
            payload = resp.json()
        content = payload["choices"][0]["message"]["content"]
        parsed = _parse_content(content)
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
        logger.warning("implementer call failed: {}: {}", type(exc).__name__, exc)
        return None
    if parsed is None:
        logger.warning("implementer returned malformed content")
    return parsed


# ------------------------------------------------------------ claim atomizing


def parse_claims(raw: dict | None, *, batch_doc_id: str) -> list[Claim]:
    """Atomize the implementer's ``{"claims": [...]}`` payload into Claims.

    Keeps claims with non-empty text. The LLM's own ``doc_id`` is PRESERVED
    when present (a hallucinated citation stays visible downstream, where
    :func:`screen_and_gate_claims` drops it on the record); a missing or
    non-string doc_id falls back to ``batch_doc_id``. The LLM's verbatim
    ``quote`` maps to ``Claim.quote_span`` when a non-blank string; a
    missing, non-string or blank quote degrades to None (the gate then
    skips the span check for that claim — tolerant by design). Output is
    capped at :data:`MAX_CLAIMS_DEEP` (first wins, input order preserved).
    ``evidence_id`` stays None — dossier packaging assigns it later.
    Malformed payloads (None, wrong shape) degrade to an empty list.
    """
    if not isinstance(raw, dict):
        return []
    entries = raw.get("claims")
    if not isinstance(entries, list):
        return []
    claims: list[Claim] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        text = entry.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        doc_id = entry.get("doc_id")
        if not isinstance(doc_id, str) or not doc_id.strip():
            doc_id = batch_doc_id
        quote = entry.get("quote")
        if not isinstance(quote, str) or not quote.strip():
            quote = None
        claims.append(Claim(text=text, doc_id=doc_id, quote_span=quote))
        if len(claims) >= MAX_CLAIMS_DEEP:
            break
    return claims


# ------------------------------------------------------------------ G4 path


def _model_name(decider) -> str:
    return getattr(decider, "model", None) or type(decider).__name__


def _drop_row_mismatch(
    decider, run_id: str, doc_id: str, index: int, claim: Claim, doc_text: str | None
) -> dict:
    """One invariant-5 ledger row for a doc_id-mismatch drop (recorded BEFORE
    any Jev spend, same shape as gate_citation_batch's deterministic drops)."""
    state = {
        "doc_id": doc_id,
        f"claim_{index}": anchor_window(doc_text, claim.text) if doc_text else claim.text,
    }
    return {
        "gate": CITATION_GATE,
        "boundary": CITATION_GATE,
        "state_hash": state_hash(state),
        "answers": None,
        "deterministic_action": "accept",
        "agree": None,
        "agree_direction": None,
        "error": None,
        "latency_ms": 0.0,
        "raw_tokens": 0,
        "model": _model_name(decider),
        "run_id": run_id,
        "called": False,
        "outcome": "dropped",
        "reason": "doc_id_mismatch",
    }


def _answer_noul(answers: dict | None, qid: str) -> float | None:
    ans = (answers or {}).get(qid)
    if isinstance(ans, dict):
        value = ans.get("noul")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _answer_choice(answers: dict | None, qid: str) -> tuple[str | None, float | None]:
    """``(label, probability)`` from a Choice answer (``{"choice",
    "probabilities"}``); ``(None, None)`` for a missing, None or malformed
    answer. The label is normalized (strip + casefold) the same way
    ``gate_citation_batch`` normalizes it. A choice without a usable
    probability for its own label yields ``(label, None)``. Sits next to
    :func:`_answer_noul` (the pre-Choice reader, kept for legacy fixtures)."""
    ans = (answers or {}).get(qid)
    if isinstance(ans, dict):
        label = ans.get("choice")
        if isinstance(label, str) and label.strip():
            label = label.strip().lower()
            prob = None
            probs = ans.get("probabilities")
            if isinstance(probs, dict):
                value = probs.get(label)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    prob = float(value)
            return label, prob
    return None, None


# The G4 Choice labels and the verdict each routes to when its probability
# clears the floor (mirrors gate_citation_batch's routing table).
_G4_VERDICTS = {
    "supports": "verified",
    "contradicted": "contradicted",
    "says_nothing": "says_nothing",
}


def _claim_floor(gates_cfg: dict | None) -> float:
    """The G4 floor (mirrors the gate's own ``_floor`` parsing so the
    verdict labels below always agree with what the gate routed)."""
    try:
        return float((gates_cfg or {}).get("floor", CLAIM_FLOOR))
    except (TypeError, ValueError):
        return CLAIM_FLOOR


def screen_and_gate_claims(
    claims: list[Claim],
    *,
    batch_doc_id: str,
    doc_text: str | None,
    decider,
    gates_cfg: dict,
    ledger,
    run_id: str,
    mode: str,
    screen_cfg: dict | None = None,
) -> list[tuple[Claim, dict]]:
    """The G4 path for one document's atomized claims.

    (1) Deterministic citation screen BEFORE any Jev spend: a claim whose
    doc_id differs from ``batch_doc_id`` is a hallucinated citation — dropped
    here and recorded on the ledger (gate ``citation_soundness``, reason
    ``doc_id_mismatch``) so the audit sees it.
    (2) Survivors are handed to :func:`gate_citation_batch`, which enforces
    doc existence (``doc_text`` None/empty), the lexical-overlap prefilter,
    the deterministic quote-span check, ONE batched Jev request, and the
    configured floor.

    Returns ``(claim, meta)`` pairs for the claims the gate RETURNED — in
    enforce mode the accepted claims only, in shadow mode every judged
    survivor (shadow is gate-inert for verdicts). ``meta`` is a dict:

    - ``"verdict"``: ``"verified"`` (supports above the floor),
      ``"contradicted"``, ``"says_nothing"``, ``"review_label_prob"``
      (probability below the floor), or ``"no_answer"`` (missing/malformed
      Choice answer — legacy noul-shaped verdicts harvest as no_answer too;
      the Choice migration retires them).
    - ``"label"`` / ``"prob"``: the Choice answer's top label and its
      probability (None when absent).
    - ``"quote_found"``: the claim carried a ``quote_span`` (the gate only
      returns claims whose span was found in the document or absent).

    ``mode`` and ``gates_cfg`` pass straight through to the gate (shadow is
    gate-inert; enforce applies the floor). ``screen_cfg`` — the
    ``decider.gates.output_screen`` block, ``None`` (the default) = the
    output screen rider is disabled — threads straight through to
    :func:`gate_citation_batch` too. Never raises: gate failures come
    back as dropped claims per the additive invariant.
    """
    survivors: list[Claim] = []
    for i, claim in enumerate(claims):
        if claim.doc_id != batch_doc_id:
            ledger.record(_drop_row_mismatch(decider, run_id, batch_doc_id, i, claim, doc_text))
            continue
        survivors.append(claim)

    accepted, decision = gate_citation_batch(
        survivors, batch_doc_id, doc_text, decider, gates_cfg, ledger, run_id, mode, screen_cfg
    )

    accepted_ids = {id(c) for c in accepted}
    floor = _claim_floor(gates_cfg)
    pairs: list[tuple[Claim, dict]] = []
    for i, claim in enumerate(survivors):
        if id(claim) not in accepted_ids:
            continue
        label, prob = _answer_choice(decision.answers, f"claim_{i}")
        if label in _G4_VERDICTS and prob is not None:
            verdict = _G4_VERDICTS[label] if prob >= floor else "review_label_prob"
        else:
            verdict = "no_answer"
        pairs.append(
            (
                claim,
                {
                    "verdict": verdict,
                    "label": label,
                    "prob": prob,
                    "quote_found": claim.quote_span is not None,
                },
            )
        )
    return pairs


# ------------------------------------------------------- five dossier fields


def extract_five_fields(
    doc_id: str,
    doc_text: str,
    *,
    model: str,
    base_url: str,
    api_key: str,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
    timeout_s: float = DEFAULT_REASONING_TIMEOUT_S,
) -> dict | None:
    """The GLM reasoning implementer for the five dossier fields.

    One call per document; the STRICT JSON output maps each of
    ``operational_need`` / ``buying_window`` / ``displacement_risk`` /
    ``expansion_signal`` / ``why_now`` to ``{"text": ..., "doc_id": ...}``,
    with ``"unknown"`` (bare string or ``{"text": "unknown"}``) for a field
    the document does not support. Returns the parsed dict or None — never
    raises, never logs the key.
    """
    messages = _build_five_fields_messages(doc_id, doc_text)
    return call_implementer(
        messages,
        model=model,
        base_url=base_url,
        api_key=api_key,
        reasoning_effort=reasoning_effort,
        timeout_s=timeout_s,
    )


def five_fields_to_claims(fields: dict | None, *, batch_doc_id: str) -> list[Claim]:
    """Convert populated five-field output into Claims.

    Only the five canonical :data:`FIVE_FIELDS` keys are considered, in that
    order. A field is kept when its value is a dict with non-empty, non-
    "unknown" text; its cited ``doc_id`` is kept when a non-empty string and
    ``batch_doc_id`` is the fallback. Bare-string "unknown", ``{"text":
    "unknown"}``, blank, missing and malformed fields are skipped. The
    resulting Claims let the SAME ``gate_citation_batch`` path gate the five
    fields (G4 applies to them per the plan; G5 does not — they are dossier
    content, not signals).
    """
    if not isinstance(fields, dict):
        return []
    claims: list[Claim] = []
    for field in FIVE_FIELDS:
        value = fields.get(field)
        if not isinstance(value, dict):
            continue
        text = value.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        if text.strip().casefold() == "unknown":
            continue
        doc_id = value.get("doc_id")
        if not isinstance(doc_id, str) or not doc_id.strip():
            doc_id = batch_doc_id
        claims.append(Claim(text=text, doc_id=doc_id))
    return claims


# ------------------------------------------------------------- candidates


def claim_natural_key(doc_id: str, text: str) -> str:
    """sha256 hexdigest of ``"{doc_id}:{normalized claim text}"``.

    A TEXT hash, NOT a claim index: LLM output ordering must not create
    duplicate signals on re-run — the same claim re-extracted yields the
    same key, so ``SignalStore`` upserts idempotently. Normalization is
    casefold + whitespace collapse.
    """
    normalized = " ".join(text.casefold().split())
    return hashlib.sha256(f"{doc_id}:{normalized}".encode("utf-8")).hexdigest()


def claims_to_candidates(
    pairs: list[tuple[Claim, dict | None]],
    *,
    domain: str,
    fetched_at: str,
    model: str,
    signal_type: str = DEFAULT_SIGNAL_TYPE,
) -> list[SignalCandidate]:
    """Build ``SignalCandidate`` objects for the signals table.

    ``pairs`` are the ``(claim, meta)`` pairs :func:`screen_and_gate_claims`
    returns (``meta`` None = the citation gate was disabled — everything
    defaults).

    - ``natural_key``: :func:`claim_natural_key` — text hash, not claim index
      (idempotent upsert on re-run).
    - ``confidence``: the Choice label probability when the verdict is
      ``"verified"``, else :data:`NO_NOUL_CONFIDENCE` (0.5 — documented
      default; normalize clamps 0..1 anyway).
    - ``evidence_data``: provenance — the cited doc_id, model id, the
      ``llm_authored`` flag, the gate that accepted the claim, the verdict,
      ``quote_found`` and ``noul`` (the label probability when present,
      else None — kept for backward compatibility with earlier readers).
    - ``observed_at``: ``fetched_at`` — the cited document's captured date;
      the claim makes no assertion about when the underlying event occurred.
    - ``title``: the claim text truncated to ~:data:`TITLE_CHARS` chars;
      ``summary``: the full claim text.

    ``domain`` is accepted for orchestrator call-site symmetry; candidates
    are domain-neutral — ``normalize_batch`` stamps ``account.domain``.
    """
    candidates: list[SignalCandidate] = []
    for claim, meta in pairs:
        meta = meta or {}
        verdict = meta.get("verdict")
        prob = meta.get("prob")
        quote_found = bool(meta.get("quote_found", claim.quote_span is not None))
        candidates.append(
            SignalCandidate(
                signal_type=signal_type,
                observed_at=fetched_at,
                natural_key=claim_natural_key(claim.doc_id, claim.text),
                title=truncate(claim.text, TITLE_CHARS),
                summary=claim.text,
                confidence=(
                    float(prob)
                    if verdict == "verified" and prob is not None
                    else NO_NOUL_CONFIDENCE
                ),
                evidence_data={
                    "doc_id": claim.doc_id,
                    "model": model,
                    "llm_authored": True,
                    "gate": CITATION_GATE,
                    "noul": prob,
                    "verdict": verdict,
                    "quote_found": quote_found,
                },
            )
        )
    return candidates


# ------------------------------------------------------------- serial runner


def implement_batch(doc_specs: list[dict], *, per_doc) -> list:
    """SERIAL loop over document specs — the v1 contract.

    Calls ``per_doc(spec)`` exactly once per spec, in input order, on the
    calling thread. MUST NOT use threads or asyncio (keeps G3/G4 and the
    cumulative token counter on the caller's thread; no new locking).
    Exists so the orchestrator wiring and tests share one serial contract.
    """
    return [per_doc(spec) for spec in doc_specs]
