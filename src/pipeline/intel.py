"""Coordinator for the master intelligence flow (Task 12).

One domain, every capability, one dossier:

    identity -> collect -> derive -> score -> package

Config and orchestrator are INJECTED. When the caller supplies them the flow
never builds its own -- deliberately NOT the shape of ``src/pipeline/sweep.py``
where ``run_sweep`` constructs a fresh ``Config.load()`` + ``Orchestrator`` and
therefore ignores the caller's config. ``config`` is also never re-read here:
every YAML/dir accessed downstream is the injected object's.

Failure posture: each stage runs in its own try/except; an exception is logged
with ``logger.exception``, recorded under ``errors[<stage>]`` and as a gap, and
the flow continues to the stages whose inputs exist. No stage error ever
escapes ``run_intel``. The ONLY ValueErrors that propagate are the bare-name
target refusal (``parse_target`` -> ``SweepError``) and the dry-run
account-existence check.

Dry run (``dry_run=True``) is genuinely non-fetching: it calls NEITHER
``resolve`` NOR ``find_careers`` NOR the derive pass (the second restricted
collect) NOR ``score`` NOR the package writer. The single permitted call is the
collection planning call with ``dry_run=True``.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from src.core.models import Account
from src.decide import gates as decide_gates
from src.decide import policy as decide_policy
from src.decide.gates import DecideLedger
from src.decide.jev import DATA_EXIT_CONSENT, LiveDecider, NullDecider, get_decider
from src.export.intel_package import build_dossier, write_intel_package
from src.intel.coverage import build_coverage
from src.intel.market import load_market_profile
from src.llm import implement as llm_implement
from src.llm import planner as llm_planner
from src.pipeline.sweep import parse_target
from src.signals.normalize import normalize_batch
from src.sources.registry import enabled_sources, sources_for_account
from src.core.textutil import truncate

__all__ = [
    "STAGES",
    "MARKETPLACE_SOURCE_KEYS",
    "LOCAL_DERIVED_SOURCES",
    "MAX_IMPLEMENT_DOCS",
    "MAX_G5_CALLS",
    "run_intel",
]

#: The five workflow stages, in execution order.
STAGES = ("identity", "collect", "derive", "score", "package")

#: The five opt-in marketplace sources (config/sources.yaml ships them all
#: ``enabled: false``); the enabling flag is ``--with-marketplaces``.
MARKETPLACE_SOURCE_KEYS = (
    "marketplace_g2",
    "marketplace_capterra",
    "marketplace_trustradius",
    "marketplace_softwareadvice",
    "marketplace_getapp",
)

#: Local-tier derived sources run as a second, restricted collection pass
#: (no HTTP) so they see the market-profile local context.
LOCAL_DERIVED_SOURCES = ("jobsignals", "needs")

#: LinkedIn-related opt-in source keys that the resolve flag covers.
LINKEDIN_OPT_IN_KEYS = ("linkedin", "linkedin_db")

#: Fallback market profile id when the caller and config name none.
DEFAULT_MARKET_PROFILE_ID = "default"

#: The repo's seller-market relevance profile file.
MARKETS_PATH = Path("config/markets.yaml")

#: Coverage statuses that mean "configured source did not run this pass".
_UNRUN_STATUSES = ("opt_in", "disabled_by_config", "backed_off", "missing_requires")

_MARKETPLACES_FLAG = "--with-marketplaces"
_LINKEDIN_FLAG = "--with-linkedin-resolve"


# --- decide layer (Task 7) constants -------------------------------------------
#
# The layer is OFF by default: with ``--with-llm`` absent (or the resolved
# decide mode "off") the only side effect below is the ONE decide.yaml config
# load, and the flow is byte-identical to the pre-layer coordinator.

#: At most this many most-recent stored documents (by fetched_at) feed the
#: implement sub-stage per run.
MAX_IMPLEMENT_DOCS = 200

#: G5 (need promotion) makes ONE Jev call per accepted candidate, so the
#: fan-out is bounded here instead of by the claim count (planned cost
#: envelope ~30 calls, 2x headroom). Candidates past the cap stay unpromoted
#: (narrow-only; deterministic needs unaffected) and the cap is gap-visible.
MAX_G5_CALLS = 60

#: ICP excerpt cap (chars) handed to the planner and to G1.
_ICP_EXCERPT_CHARS = 1200

#: Token cap for ONE document's contribution to the pre-flight projection.
#: The G4 anchor state is head (~100 tokens) + anchor window (~600); 2400
#: leaves generous headroom.
_TOKEN_CAP_PER_DOC = 2400

#: Per-gate allowances for the pre-flight projection, modelling the REAL
#: call structure: the document gate (wave-1) makes ONE request per staged
#: doc over the same capped text, G4 makes one batched Jev call PER document
#: (its input state is the per-doc cap above plus call overhead), G1 is one
#: batched call over the plan steps, G2 is one posture call, and G5 makes one
#: call per accepted candidate bounded by :data:`MAX_G5_CALLS` at ~300 tokens
#: per call. A documented cost envelope, not an exact token count.
_G1_TOKEN_ALLOWANCE = 700
_G2_TOKEN_ALLOWANCE = 700
_G5_TOKEN_ALLOWANCE = MAX_G5_CALLS * 300

#: The completeness-verify cascade's per-request allowance (wave-2): ONE
#: verify request for the longest doc (v1 verifies only the LONGEST doc)
#: plus ONE re-verification per potential escalation, up to max_escalations
#: of them — the projection bills that worst case, ~700 tokens each.
_COMPLETENESS_TOKEN_ALLOWANCE = 700

#: The taxonomy-typing battery's flat per-doc allowance (wave-2): ONE typing
#: request per doc WITH accepted claims — doc-capped claim excerpts plus a
#: 54-option criteria block per claim. Acceptance is known only AFTER G4,
#: which runs after this projection, so the term is counted for EVERY staged
#: doc (docs that yield no accepted claims cost nothing but are counted) and
#: the flat ~800 stays an honest upper bound for the post-gate claim sparsity
#: ("usually few after gating"); a doc that kept many claims can exceed it —
#: the ceiling's whole-run shadow degradation is the safety net, not this
#: envelope, an approximation by design like every allowance here.
_TYPING_TOKEN_ALLOWANCE = 800

#: The event-date battery's per-request allowance (wave-2): ONE date request
#: per doc WITH accepted claims at ~700 tokens (the seven-question Choice
#: battery with the full year criteria). Claim-bearing-ness is known only
#: AFTER G4, so — like the typing term — the flat per-staged-doc allowance
#: stays an honest upper bound, not an exact count.
_EVENT_DATES_TOKEN_ALLOWANCE = 700

#: The value-extraction battery's per-request allowance (wave-2): ONE small
#: request per money-bearing accepted claim (~200 tokens: the claim text, the
#: regex spans as Choice options, two questions). The loop bills PER CLAIM,
#: so the pre-flight's honest worst case for a doc is the bulk extract's own
#: per-doc claim cap (:data:`src.llm.implement.MAX_CLAIMS_DEEP`) times this
#: allowance — see the projection below.
_VALUE_EXTRACTION_TOKEN_ALLOWANCE = 200

#: Default cap on completeness escalations per run (config/decide.yaml
#: ``decider.gates.completeness_verify.max_escalations``).
_DEFAULT_MAX_ESCALATIONS = 20

#: Whole-run decide-token ceiling default (config/decide.yaml
#: ``decider.max_decide_tokens_per_run``).
_DEFAULT_MAX_DECIDE_TOKENS = 400000


def _load_decide_cfg(cfg) -> dict:
    """``config/decide.yaml``, or ``{}`` when absent.

    This is the ONE config load the off path makes. A file that exists but
    cannot be parsed degrades to ``{}`` (mode off, deterministic run) with a
    warning — the layer can never crash the run.
    """
    try:
        return cfg.load_yaml("decide") or {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("decide layer: config/decide.yaml unreadable ({}); treating as off", exc)
        return {}


def _gate_cfg(decide_cfg: dict, name: str) -> dict:
    """The ``decider.gates.<name>`` block of config/decide.yaml (``{}`` when
    absent — every gate then uses its documented defaults)."""
    decider_cfg = (decide_cfg or {}).get("decider") or {}
    gates = decider_cfg.get("gates") or {}
    block = gates.get(name)
    return dict(block) if isinstance(block, dict) else {}


def _gate_enabled(decide_cfg: dict, name: str) -> bool:
    """Whether ``decider.gates.<name>`` is enabled (default True).

    A gate disabled by config is SKIPPED at the call site: the deterministic
    path proceeds for that boundary and no ledger rows are produced.
    """
    return bool(_gate_cfg(decide_cfg, name).get("enabled", True))


def _decode_body(body) -> str:
    """Stored body bytes -> text with a replacement policy; never raises.

    Mirrors src/sources/needs/collector.py's ``_decode``: RawStore.get()
    gunzips bytes; a missing blob (None) or a defensive str body are handled
    too.
    """
    if body is None:
        return ""
    if isinstance(body, (bytes, bytearray)):
        return bytes(body).decode("utf-8", errors="replace")
    return str(body)


def _g1_drop_reason(decision) -> str:
    """Why gate_plan_step dropped a plan step, derived from its Decision.

    gates.py drops on ``ok=False`` (Jev error), a missing/invalid noul (no
    verdict), or a noul below the configured floor — an applicable, ok
    Decision with a numeric noul can ONLY have been dropped by the floor.
    """
    if not decision.ok:
        return "Jev error"
    ans = (decision.answers or {}).get("step")
    noul = ans.get("noul") if isinstance(ans, dict) else None
    if isinstance(noul, (int, float)) and not isinstance(noul, bool):
        return "below floor"
    return "no verdict"


def _identity_state(domain: str, account, profile_id: str) -> dict:
    """The identity fields the planner's snapshot hash and ICP excerpt cover."""
    return {
        "domain": domain,
        "name": getattr(account, "name", None),
        "ats_vendor": getattr(account, "ats_vendor", None),
        "careers_url": getattr(account, "careers_url", None),
        "cik": getattr(account, "cik", None),
        "feed_url": getattr(account, "blog_feed_url", None),
        "icp_fit": getattr(account, "icp_fit", None),
        "profile_id": profile_id,
    }


def _utc_now_iso() -> str:
    """Mirror of orchestrator ``_now()``: aware-UTC ISO, no microseconds.

    Duplicated (not imported) so the intel wiring never imports the
    orchestrator module — the same "now" convention, no new coupling.
    """
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _shadow_appendix(rows: list[dict]) -> dict:
    """Per-gate agreement appendix computed from ledger rows: the fraction of
    judged rows that agreed plus the agree_direction histogram (invariant 5:
    agreement rate alone conflates added strictness with added recall loss)."""
    by_gate: dict[str, list[dict]] = {}
    for row in rows or ():
        by_gate.setdefault(str(row.get("gate")), []).append(row)
    appendix: dict[str, dict] = {}
    for gate, gate_rows in by_gate.items():
        judged = [r for r in gate_rows if r.get("agree") is not None]
        directions: dict[str, int] = {}
        for r in gate_rows:
            direction = r.get("agree_direction")
            if direction:
                directions[direction] = directions.get(direction, 0) + 1
        appendix[gate] = {
            "agree_rate": (
                sum(1 for r in judged if r.get("agree")) / len(judged)
            )
            if judged
            else None,
            "agree_directions": dict(sorted(directions.items())),
        }
    return appendix


def _implement_pass(
    *,
    orc,
    account,
    domain: str,
    decider,
    decide_cfg: dict,
    ledger,
    run_id: str,
    mode: str,
) -> dict:
    """The implement sub-stage body: G3 routing, bulk claim extraction, G4
    citation gating, five G4-gated dossier fields, G5-gated promotion.

    Returns ``{"docs", "docs_screened", "docs_excluded", "claims_accepted",
    "promoted", "llm_fields", "degraded", "projected_tokens",
    "token_ceiling", "gaps", "five_fields_escalations",
    "five_fields_quarantined", "claims_typed", "event_dates_extracted",
    "amounts_extracted"}``. NEVER raises past the caller's try/except;
    degradation is expressed by return value, never by aborting the run.
    """
    result: dict = {
        "docs": 0,
        "docs_screened": 0,
        "docs_excluded": 0,
        "docs_reranked": 0,
        "claims_accepted": 0,
        "claims_typed": 0,
        "event_dates_extracted": 0,
        "amounts_extracted": 0,
        "promoted": 0,
        "llm_fields": {},
        "degraded": False,
        "projected_tokens": 0,
        "token_ceiling": 0,
        "gaps": [],
        "five_fields_escalations": 0,
        "five_fields_quarantined": 0,
    }
    raw_store = orc.raw
    taxonomy = getattr(orc, "taxonomy", None)
    signal_store = getattr(orc, "signal_store", None)
    if taxonomy is None or signal_store is None:
        result["gaps"].append(
            "llm promotion unavailable (no taxonomy or signal store in scope); "
            "gated claims stay recorded but are never promoted"
        )

    impl_cfg = decide_cfg.get("implementers") or {}
    bulk_cfg = impl_cfg.get("bulk") or {}
    reasoning_cfg = impl_cfg.get("reasoning") or {}
    bulk_model = str(bulk_cfg.get("model") or "")
    bulk_base_url = str(bulk_cfg.get("base_url") or "")
    fields_model = str(reasoning_cfg.get("model") or "")
    api_key = os.environ.get("OPENROUTER_API_KEY")
    try:
        bulk_timeout = float(bulk_cfg.get("timeout_s", 60.0))
    except (TypeError, ValueError):
        bulk_timeout = 60.0

    # Load the stored docs BEFORE any Jev/LLM call in this sub-stage: the
    # token pre-flight needs them. Most recent MAX_IMPLEMENT_DOCS by
    # fetched_at (RawStore orders ascending, so keep the tail). RawStore's
    # iter_docs is METADATA-ONLY (body stays None — only get() gunzips the
    # stored bytes), so each body is loaded through raw_store.get() and
    # decoded once; empty bodies are skipped — a document with no text
    # cannot support a claim.
    staged: list[object] = list(raw_store.iter_docs(domain=domain))
    staged.sort(key=lambda doc: str(getattr(doc, "fetched_at", "") or ""))
    staged = staged[-MAX_IMPLEMENT_DOCS:]
    docs: list[tuple[object, str]] = []
    for meta_doc in staged:
        doc_id = str(getattr(meta_doc, "doc_id", "") or "")
        full = None
        if doc_id:
            try:
                full = raw_store.get(doc_id)
            except Exception:
                logger.debug("llm implementers: body unreadable for {}", doc_id)
                full = None
        text = _decode_body(getattr(full, "body", None) if full is not None else None)
        if not text.strip():
            continue
        docs.append((full if full is not None else meta_doc, text))
    result["docs"] = len(docs)
    if staged and not docs:
        # A silent no-op is never acceptable: documents ARE in scope, so the
        # stage must say why it produced nothing.
        result["gaps"].append(
            f"llm implementers: {len(staged)} documents in scope but 0 bodies loadable; "
            "stage no-op"
        )

    # Token pre-flight (a documented cost envelope, see the module constants):
    # every loaded document's gate state capped at _TOKEN_CAP_PER_DOC plus
    # per-gate allowances (G1/G2 one call each, G5 bounded by MAX_G5_CALLS).
    # The document gate (wave-1) adds ONE Jev request per staged doc over the
    # same capped text — the term is counted here, BEFORE the first Jev call
    # of the run, so the whole-run shadow degradation stays honest. Over the
    # ceiling the WHOLE RUN degrades to shadow — never a mid-run abort (a
    # partially-gated dossier is not comparable to anything).
    # NOTE the ordering: the plan sub-stage's G1 spend (bounded by
    # max_plan_steps) has already happened by the time this projection runs,
    # so the ceiling binds everything from here on; the degradation flag makes
    # that visible in the artifact (invariant 3).
    per_doc_tokens = [
        min(decide_gates.estimate_tokens(text), _TOKEN_CAP_PER_DOC) for _doc, text in docs
    ]
    projected = sum(per_doc_tokens)
    if _gate_enabled(decide_cfg, "document_gate"):
        projected += sum(per_doc_tokens)  # one doc-gate request per staged doc
    # Completeness-verify cascade (wave-2): OPT-IN — the term (and the gate
    # below) is counted only when the config block EXISTS and is enabled, so
    # cfgs without the block (every pre-wave-2 config) keep today's exact
    # projection. The worst case the cascade can actually bill: ONE verify
    # request for the longest doc plus one RE-verification per escalation,
    # up to max_escalations of them, ~700 tokens each.
    completeness_cfg = _gate_cfg(decide_cfg, "completeness_verify")
    completeness_on = bool(completeness_cfg) and completeness_cfg.get("enabled", True)
    try:
        max_escalations = int(completeness_cfg.get("max_escalations", _DEFAULT_MAX_ESCALATIONS))
    except (TypeError, ValueError):
        max_escalations = _DEFAULT_MAX_ESCALATIONS
    if completeness_on and docs and fields_model:
        projected += (1 + max_escalations) * _COMPLETENESS_TOKEN_ALLOWANCE
    # Taxonomy typing (wave-2): OPT-IN like the cascade — the term (and the
    # battery below) is counted only when the config block EXISTS and is
    # enabled, so cfgs without the block keep today's exact projection. ONE
    # request per doc WITH accepted claims; see _TYPING_TOKEN_ALLOWANCE for
    # why the flat per-staged-doc term stays an honest upper bound.
    typing_cfg = _gate_cfg(decide_cfg, "taxonomy_typing")
    typing_on = bool(typing_cfg) and typing_cfg.get("enabled", True)
    if typing_on and docs:
        projected += len(docs) * _TYPING_TOKEN_ALLOWANCE
    # Event dates (wave-2): OPT-IN like the cascade and typing — the term
    # (and the battery below) is counted only when the config block EXISTS
    # and is enabled, so cfgs without the block keep today's exact
    # projection. ONE date request per doc WITH accepted claims; acceptance
    # is known only after G4, so every staged doc is counted (the typing
    # term's documented honest upper bound).
    dates_cfg = _gate_cfg(decide_cfg, "event_dates")
    dates_on = bool(dates_cfg) and dates_cfg.get("enabled", True)
    if dates_on and docs:
        projected += len(docs) * _EVENT_DATES_TOKEN_ALLOWANCE
    # Value extraction (wave-2): OPT-IN like the cascade, typing and dates —
    # the term (and the battery below) is counted only when the config block
    # EXISTS and is enabled, so cfgs without the block keep today's exact
    # projection. The battery bills PER MONEY-BEARING ACCEPTED claim (one
    # small ~200-token request each); money-bearing-ness is known only after
    # G4 + the regex scan, so the honest worst case is the bulk extract's own
    # per-doc claim cap multiplied out: len(docs) * MAX_CLAIMS_DEEP requests.
    amounts_cfg = _gate_cfg(decide_cfg, "value_extraction")
    amounts_on = bool(amounts_cfg) and amounts_cfg.get("enabled", True)
    if amounts_on and docs:
        projected += (
            len(docs) * llm_implement.MAX_CLAIMS_DEEP * _VALUE_EXTRACTION_TOKEN_ALLOWANCE
        )
    projected += _G1_TOKEN_ALLOWANCE + _G2_TOKEN_ALLOWANCE + _G5_TOKEN_ALLOWANCE
    decider_cfg = decide_cfg.get("decider") or {}
    try:
        ceiling = int(decider_cfg.get("max_decide_tokens_per_run", _DEFAULT_MAX_DECIDE_TOKENS))
    except (TypeError, ValueError):
        ceiling = _DEFAULT_MAX_DECIDE_TOKENS
    result["projected_tokens"] = projected
    result["token_ceiling"] = ceiling
    if projected > ceiling:
        result["degraded"] = True
        mode = "shadow"  # from here on the layer is shadow-only
        result["gaps"].append(
            f"decide token budget exceeded ({projected} > {ceiling}); "
            "degraded to shadow for the whole run"
        )

    if not docs:
        return result
    if not bulk_model:
        result["gaps"].append("llm implementers unavailable (no bulk model configured)")
        return result

    # Document gate (wave-1): screen each staged doc BEFORE any implementer
    # spend — an excluded doc NEVER enters doc_specs, so the bulk extraction
    # (the dominant layer cost) never runs for it. The gate is disabled-by-
    # config SKIPPED here (the per-gate enabled pattern: deterministic path
    # proceeds, no rows); a NullDecider yields not-applicable include rows.
    doc_gate_cfg = _gate_cfg(decide_cfg, "document_gate")
    # Evidence re-rank (wave-3): the re-rank head RIDES the doc-gate request
    # (zero marginal requests), so it is INERT without the doc gate — one
    # debug log when it is asked for but the doc gate is off. OPT-IN pattern
    # (completeness/typing/dates): the evidence_rerank block must EXIST and be
    # enabled — _gate_enabled defaults absent blocks ON, which would silently
    # re-rank every pre-wave-3 config — AND the doc gate must be enabled with
    # a decider in scope (exactly when the screening block below runs).
    rerank_cfg = _gate_cfg(decide_cfg, "evidence_rerank")
    rerank_requested = bool(rerank_cfg) and bool(rerank_cfg.get("enabled", True))
    doc_gate_on = bool(doc_gate_cfg.get("enabled", True))
    rerank_active = rerank_requested and doc_gate_on and decider is not None
    if rerank_requested and not doc_gate_on:
        logger.debug(
            "evidence_rerank enabled but document_gate disabled; re-rank inert (run={})",
            run_id,
        )
    account_name = str(getattr(account, "name", None) or domain)
    if doc_gate_on and decider is not None:
        excluded_reasons: dict[str, int] = {}
        screened: list[tuple[object, str, float | None]] = []
        for doc, text in docs:
            include, _decision, strength = decide_gates.gate_document(
                str(getattr(doc, "doc_id", "") or ""),
                text,
                account_name,
                decider,
                doc_gate_cfg,
                ledger,
                run_id,
                mode,
                rerank=rerank_active,
            )
            if include:
                screened.append((doc, text, strength))
            else:
                # The gate just recorded the row; the exclusion reason rides
                # on it for the stage-record breakdown.
                reason = str((ledger.rows[-1].get("reason") if ledger.rows else None) or "unknown")
                excluded_reasons[reason] = excluded_reasons.get(reason, 0) + 1
        result["docs_screened"] = len(docs)
        result["docs_excluded"] = len(docs) - len(screened)
        if excluded_reasons:
            result["docs_excluded_reasons"] = excluded_reasons
        ranked = screened
        if not screened:
            # All staged docs excluded in enforce: the implement pass is a
            # documented no-op — the deterministic derive output is unaffected
            # (this stage is additive) and the run completes.
            result["gaps"].append(
                f"document gate excluded all {result['docs_screened']} staged documents; "
                "implement pass no-op"
            )
            return result
    else:
        ranked = [(doc, text, None) for doc, text in docs]
    if rerank_active:
        # The head rode EVERY gate request (both modes): docs_reranked counts
        # the included docs carrying a usable strength — Jev errors,
        # NullDeciders and missing heads stay None and are never invented.
        result["docs_reranked"] = sum(1 for _doc, _text, s in ranked if s is not None)

    # Evidence re-rank (wave-3): ORDERING + keep_n cap, ENFORCE-ONLY. Shadow
    # keeps fetched_at order and never caps (the rows still record the
    # strength — a clean A/B). Ungraded docs (Jev error / NullDecider /
    # missing head) carry strength None: they sort LAST — the keep_order
    # posture — and an all-None run keeps fetched_at order because sorted()
    # is stable. The cap applies to the ordered list REGARDLESS of
    # gradedness, so None-strength docs are the first a binding cap cuts;
    # each capped-out doc gets a document_gate row (reason "rerank_cap",
    # state_hash joined to its include row) and joins docs_excluded.
    if rerank_active and mode == "enforce":
        ranked = sorted(ranked, key=lambda triple: (triple[2] is None, -(triple[2] or 0.0)))
        try:
            keep_n = int(rerank_cfg.get("keep_n", 0))
        except (TypeError, ValueError):
            keep_n = 0
        if keep_n > 0 and len(ranked) > keep_n:
            capped_out = ranked[keep_n:]
            ranked = ranked[:keep_n]
            for doc, text, _strength in capped_out:
                ledger.record(
                    decide_gates.rerank_cap_row(
                        str(getattr(doc, "doc_id", "") or ""),
                        text,
                        account_name,
                        decider,
                        run_id,
                    )
                )
            result["docs_excluded"] += len(capped_out)
            reasons = result.setdefault("docs_excluded_reasons", {})
            reasons["rerank_cap"] = reasons.get("rerank_cap", 0) + len(capped_out)
    docs = [(doc, text) for doc, text, _strength in ranked]

    doc_specs = [
        {
            "doc_id": getattr(doc, "doc_id", ""),
            "text": text,
            "fetched_at": getattr(doc, "fetched_at", None),
        }
        for doc, text in docs
    ]

    # Gate configs are resolved once; a gate disabled by config is SKIPPED at
    # its call site below (Q: the deterministic path proceeds, no rows).
    routing_cfg = _gate_cfg(decide_cfg, "routing")
    citation_cfg = _gate_cfg(decide_cfg, "citation_soundness")
    promotion_cfg = _gate_cfg(decide_cfg, "need_promotion")
    # The output screen RIDES the citation gate's per-document request: its
    # config block threads through screen_and_gate_claims -> gate_citation_batch.
    screen_cfg = _gate_cfg(decide_cfg, "output_screen")

    def _heuristic_route(text: str) -> str:
        """G3's deterministic heuristic alone (gate disabled: no Jev consult)."""
        try:
            threshold = int(routing_cfg.get("route_threshold", decide_gates.ROUTE_THRESHOLD))
        except (TypeError, ValueError):
            threshold = decide_gates.ROUTE_THRESHOLD
        return "deep" if decide_gates.estimate_tokens(text) >= threshold else "quick"

    def _per_doc(spec: dict) -> list:
        if routing_cfg.get("enabled", True):
            route = decide_gates.gate_routing(
                decide_gates.estimate_tokens(spec["text"]),
                decider,
                routing_cfg,
                ledger,
                run_id,
                mode,
            )
        else:
            route = _heuristic_route(spec["text"])
        raw = llm_implement.call_implementer(
            llm_implement.build_extraction_messages(spec["doc_id"], spec["text"], route=route),
            model=bulk_model,
            base_url=bulk_base_url,
            api_key=api_key,
            timeout_s=bulk_timeout,
        )
        claims = llm_implement.parse_claims(raw, batch_doc_id=spec["doc_id"])
        if not citation_cfg.get("enabled", True):
            # Citation gate disabled: the deterministic baseline stands —
            # implementer claims are additive, so they are accepted ungated
            # (no Jev, no rows, noul None -> the documented default confidence).
            return [(claim, None) for claim in claims]
        return llm_implement.screen_and_gate_claims(
            claims,
            batch_doc_id=spec["doc_id"],
            doc_text=spec["text"],
            decider=decider,
            gates_cfg=citation_cfg,
            ledger=ledger,
            run_id=run_id,
            mode=mode,
            screen_cfg=screen_cfg,
        )

    # SERIAL in v1 (the shared llm_implement contract): the loop stays on the
    # caller's thread so the gates and the token budget do too.
    per_doc_pairs = llm_implement.implement_batch(doc_specs, per_doc=_per_doc)

    doc_text_by_id = {
        str(getattr(doc, "doc_id", "") or ""): text for doc, text in docs
    }
    promotable: list = []
    g5_calls = 0
    g5_capped = False
    for spec, pairs in zip(doc_specs, per_doc_pairs):
        result["claims_accepted"] += len(pairs)
        if not promotion_cfg.get("enabled", True):
            # Promotion gate disabled: LLM candidates have NO deterministic
            # promotion, so nothing is attempted (deterministic needs, which
            # never pass through here, are unaffected).
            continue
        # Taxonomy typing (wave-2): ONE Jev request per doc over the claims
        # that survived G4 for THIS doc (in enforce, exactly the accepted
        # ones). Enrichment only — the proposals never change a candidate's
        # signal_type; in shadow the battery still runs but returns no
        # proposals (enforce-only enrichment, a clean A/B). Disabled by
        # config (the wave-2 opt-in) or no taxonomy in scope: skipped, zero
        # delta to the pre-typing behavior.
        proposals: dict[int, dict] = {}
        if typing_on and taxonomy is not None and decider is not None and pairs:
            proposals = decide_gates.classify_claims(
                [claim for claim, _meta in pairs],
                spec["text"],
                taxonomy,
                decider,
                typing_cfg,
                ledger,
                run_id,
                mode,
            )
            if proposals:
                result["claims_typed"] += len(proposals)
        # Event dates (wave-2): ONE Jev request for THIS doc when it produced
        # at least one accepted claim (bounds requests to yielding docs).
        # The model only classifies the stated date; code assembles it
        # against the document's fetched_at (the gate's pinned reference).
        # Enrichment only; in shadow the battery runs rows-only and returns
        # nothing to thread (enforce-only enrichment, a clean A/B).
        event: dict = {}
        if dates_on and decider is not None and pairs:
            event = decide_gates.extract_event_date(
                spec["doc_id"],
                spec["text"],
                spec["fetched_at"],
                decider,
                dates_cfg,
                ledger,
                run_id,
                mode,
            )
            if event.get("event_at"):
                result["event_dates_extracted"] += 1
        # Value extraction (wave-2): ONE small Jev request per money-bearing
        # ACCEPTED claim (regex proposes spans, the model picks among them,
        # code normalizes — the picked span is always a verbatim span). Claims
        # without spans make NO request and NO row (the gate returns before
        # calling). Enrichment only; in shadow the requests still run
        # rows-only and return nothing to thread (enforce-only enrichment,
        # a clean A/B).
        amounts: dict[int, dict] = {}
        if amounts_on and decider is not None and pairs:
            for idx, (claim, _meta) in enumerate(pairs):
                amount = decide_gates.extract_claim_amount(
                    claim.text, decider, amounts_cfg, ledger, run_id, mode
                )
                if amount is not None:
                    amounts[idx] = amount
            if amounts:
                result["amounts_extracted"] += len(amounts)
        candidates = llm_implement.claims_to_candidates(
            pairs,
            domain=domain,
            fetched_at=spec["fetched_at"],
            model=bulk_model,
            proposals=proposals or None,
        )
        # Thread the doc-level date enrichment into every candidate built
        # from THIS doc's claims: event_at (ISO, only when truthy) and
        # event_at_needs_review (only when True). observed_at STAYS the
        # document's fetched_at (the map's carrier decision — no schema
        # change, no decay change); the builder owns the evidence_data
        # assembly, so this post-hoc fold stays additive and local.
        if event:
            event_at = event.get("event_at")
            needs_review = bool(event.get("needs_review"))
            if event_at or needs_review:
                for cand in candidates:
                    evidence = getattr(cand, "evidence_data", None)
                    if isinstance(evidence, dict):
                        if event_at:
                            evidence["event_at"] = event_at
                        if needs_review:
                            evidence["event_at_needs_review"] = True
        # Thread the per-claim amount enrichment into the candidate built from
        # THAT claim: claims_to_candidates builds one candidate per (claim,
        # meta) pair IN ORDER, so the amount keyed by pair index maps onto
        # candidates[idx].evidence_data (the per-claim parallel of the
        # doc-level event fold above; only claims with a result gain fields).
        # No FX in v1: amount_usd carries the face value and amount_currency
        # names it.
        for idx, amount in amounts.items():
            if idx >= len(candidates):
                continue
            evidence = getattr(candidates[idx], "evidence_data", None)
            if isinstance(evidence, dict):
                evidence["amount_display"] = amount["amount_display"]
                evidence["amount_usd"] = amount["amount_usd"]
                evidence["amount_currency"] = amount["currency"]
                evidence["amount_kind"] = amount["kind"]
        for cand in candidates:
            if g5_calls >= MAX_G5_CALLS:
                # G5's per-candidate call budget is spent for this run: the
                # remaining candidates stay unpromoted (narrow-only) and the
                # cap becomes gap-visible below.
                g5_capped = True
                continue
            g5_calls += 1
            need_text = cand.summary or cand.title or ""
            cited_text = doc_text_by_id.get(
                str((cand.evidence_data or {}).get("doc_id") or "")
            )
            # The judge sees the CITED DOCUMENT — anchor-windowed around the
            # claim — never merely the claim's own echo; the claim text is
            # the fallback when the cited body is not in scope.
            evidence_excerpt = (
                decide_gates.anchor_window(cited_text, need_text)
                if cited_text
                else need_text
            )
            promote, prob, _decision = decide_gates.gate_need_promotion(
                need_text,
                evidence_excerpt,
                False,  # deterministic_promote: LLM candidates have no deterministic promotion
                decider,
                promotion_cfg,
                ledger,
                run_id,
                mode,
            )
            # Enforce + promote flows through; shadow NEVER promotes
            # (deterministic-only) — the row carries the would-be verdict.
            if promote and mode == "enforce":
                if prob is not None:
                    cand.confidence = prob  # the noul IS the stored probability
                promotable.append(cand)

    if g5_capped:
        result["gaps"].append(
            f"G5 call budget cap reached ({MAX_G5_CALLS}); "
            "remaining LLM candidates not promoted this run"
        )

    if promotable and taxonomy is not None and signal_store is not None:
        valid, rejected = normalize_batch(
            promotable,
            account=account,
            source="llm_implement",
            taxonomy=taxonomy,
            now=_utc_now_iso(),
            raw_ref=None,
        )
        for _cand, why in rejected:
            logger.debug("llm candidate rejected by normalize: {}", why)
        new, _updated = signal_store.upsert_many(valid)
        result["promoted"] = len(valid)
    elif promotable:
        logger.debug("llm candidates gated for promotion but no taxonomy/signal store; dropped")

    # --- five dossier fields (reasoning implementer, G4-gated; G5 does NOT
    # apply — they are dossier content, not signals) --------------------------
    if docs and fields_model:
        longest_doc, longest_text = max(docs, key=lambda pair: len(pair[1]))
        longest_doc_id = str(getattr(longest_doc, "doc_id", "") or "")
        try:
            reasoning_timeout = float(
                reasoning_cfg.get("timeout_s", llm_implement.DEFAULT_REASONING_TIMEOUT_S)
            )
        except (TypeError, ValueError):
            reasoning_timeout = llm_implement.DEFAULT_REASONING_TIMEOUT_S

        def _extract_and_screen() -> tuple[dict | None, dict[str, dict]]:
            """One reasoning-model pass for the longest doc: extract the five
            fields, convert them to claims, run the SAME G4 screen, and
            return (raw fields, field -> dossier entry) for the fields that
            survived. The escalation re-run reuses this exact call shape and
            credentials path."""
            fields = llm_implement.extract_five_fields(
                longest_doc_id,
                longest_text,
                model=fields_model,
                base_url=str(reasoning_cfg.get("base_url") or ""),
                api_key=os.environ.get("OPENROUTER_API_KEY"),
                reasoning_effort=str(
                    reasoning_cfg.get("reasoning_effort", llm_implement.DEFAULT_REASONING_EFFORT)
                ),
                timeout_s=reasoning_timeout,
            )
            # Mirror five_fields_to_claims' keep-filter (same order, same
            # rules — five_field_is_present IS that filter): it emits ONE
            # claim per kept field, in FIVE_FIELDS order, so each claim can
            # be mapped back to its canonical field name BY CLAIM IDENTITY.
            # A positional zip against the gate's surviving pairs would
            # mis-pair every field after any gate drop (prefilter/floor).
            kept_fields = [
                field
                for field in llm_implement.FIVE_FIELDS
                if decide_gates.five_field_is_present(fields, field)
            ]
            field_claims = llm_implement.five_fields_to_claims(
                fields, batch_doc_id=longest_doc_id
            )
            field_by_claim = {id(claim): field for field, claim in zip(kept_fields, field_claims)}
            if citation_cfg.get("enabled", True):
                gated_field_pairs = llm_implement.screen_and_gate_claims(
                    field_claims,
                    batch_doc_id=longest_doc_id,
                    doc_text=longest_text,
                    decider=decider,
                    gates_cfg=citation_cfg,
                    ledger=ledger,
                    run_id=run_id,
                    mode=mode,
                    screen_cfg=screen_cfg,
                )
            else:
                # Citation gate disabled: the deterministic baseline (accept)
                # stands — see _per_doc.
                gated_field_pairs = [(claim, None) for claim in field_claims]
            # (claim, meta) pairs — the meta carries the Choice verdict; the
            # wiring (filtering by verdict, quote provenance) lands in Task 4.
            filled: dict[str, dict] = {}
            for claim, _meta in gated_field_pairs:
                field = field_by_claim.get(id(claim))
                if field is None:
                    # Defensive: the filters above mirror each other, so every
                    # surviving claim has a field; never invent a name.
                    continue
                filled[field] = {
                    "text": claim.text,
                    "doc_id": claim.doc_id,
                    "llm_authored": True,
                    "model": fields_model,
                }
            return fields, filled

        fields, filled = _extract_and_screen()
        result["llm_fields"].update(filled)

        # Completeness-verify cascade (wave-2): the battery judges the RAW
        # extract (the caller interpretation matches on the same present/
        # unknown rule the gate used). Fired PRESENT fields are QUARANTINED —
        # a hallucinated fill must not survive (G4 never judged the raw
        # fill). Fired UNKNOWN fields ESCALATE: one reasoning-model re-run
        # (re-screened through the same G4 path) replaces the fired unknown
        # fields it fills, marked "escalated"; the re-run is then
        # RE-VERIFIED, so a doc that stays unknown re-escalates until the
        # per-run cap. Shadow rows record the verdict while the gate returns
        # an empty fired list — this block then changes nothing.
        # A FAILED extract (None — the never-raise contract) is NOT five
        # honest unknowns: judging it would escalate futilely (the two
        # providers can disagree in health) and misattribute the closing
        # gap, so the cascade is skipped with its own gap line instead.
        if fields is None:
            result["gaps"].append(
                "five-fields extraction failed; completeness cascade skipped"
            )
        elif completeness_on:
            escalations = 0
            quarantined = 0
            while True:
                fired, _decision = decide_gates.gate_completeness(
                    longest_doc_id,
                    longest_text,
                    fields,
                    decider,
                    completeness_cfg,
                    ledger,
                    run_id,
                    mode,
                )
                if not fired:
                    break
                present_fired = [
                    f for f in fired if decide_gates.five_field_is_present(fields, f)
                ]
                unknown_fired = [f for f in fired if f not in present_fired]
                for field in present_fired:
                    # Quarantine ONLY the fill this pass's judgment covers:
                    # the stored entry's text must equal the judged RAW text.
                    # An escalation re-run's re-write of a previously
                    # grounded field was never judged (its good fill
                    # survives), and an already-quarantined or never-stored
                    # field is a no-op pop that must not re-increment.
                    raw_text = (fields.get(field) or {}).get("text")
                    entry = result["llm_fields"].get(field)
                    if not isinstance(entry, dict) or entry.get("text") != raw_text:
                        continue
                    result["llm_fields"].pop(field, None)
                    quarantined += 1
                    logger.debug(
                        "completeness: quarantined field {} of {} (run={})",
                        field,
                        longest_doc_id,
                        run_id,
                    )
                if not unknown_fired:
                    break
                if escalations >= max_escalations:
                    result["gaps"].append(
                        f"completeness escalation cap reached ({max_escalations}); "
                        "remaining docs unverified"
                    )
                    break
                escalations += 1
                fields, refilled = _extract_and_screen()
                if fields is None:
                    # The re-run FAILED the same way (never-raise contract):
                    # judging its None would read as five fresh unknowns and
                    # escalate futilely while the providers disagree in
                    # health — stop here with the honest cause.
                    result["gaps"].append(
                        "five-fields extraction failed; completeness cascade skipped"
                    )
                    break
                for field in unknown_fired:
                    if field in refilled:
                        entry = dict(refilled[field])
                        entry["escalated"] = True
                        result["llm_fields"][field] = entry
            if escalations:
                result["five_fields_escalations"] = escalations
            if quarantined:
                result["five_fields_quarantined"] = quarantined
    elif docs and not fields_model:
        result["gaps"].append("llm field extraction unavailable (no reasoning model configured)")

    return result


def _load_config():
    from src.core.config import Config

    return Config.load()


def _build_orchestrator(cfg):
    from src.pipeline.orchestrator import Orchestrator

    return Orchestrator(cfg)


def _profile_id(explicit, cfg) -> str:
    """Explicit arg, else a top-level config override, else the literal default."""
    if explicit:
        return explicit
    override = getattr(cfg, "market_profile_id", None)
    if override:
        return str(override)
    return DEFAULT_MARKET_PROFILE_ID


def _load_profile(profile_id, gaps):
    """Load the market profile; fail soft to ``None`` and record a gap.

    An unknown profile id (``ConfigError``) or an empty profile is NOT fatal:
    the run continues with ``market_profile=None`` so the ``needs`` source
    emits nothing, and a gap explains that no relevance vocabulary is
    configured so nothing can be promoted.
    """
    try:
        profile = load_market_profile(profile_id, path=MARKETS_PATH)
    except Exception as exc:
        gaps.append(
            f"no relevance vocabulary configured: market profile {profile_id!r} "
            f"could not be loaded ({exc}); nothing can be promoted"
        )
        return None
    if getattr(profile, "is_empty", False):
        gaps.append(
            f"no relevance vocabulary configured: market profile {profile_id!r} "
            "is empty; nothing can be promoted"
        )
        return None
    return profile


def _outcomes(stats) -> list:
    """RunnerStats.outcomes (or a dict-shaped stub's) as a list."""
    if isinstance(stats, dict):
        return list(stats.get("outcomes") or ())
    return list(getattr(stats, "outcomes", None) or ())


def _stat(stats, name) -> int:
    if isinstance(stats, dict):
        return int(stats.get(name, 0) or 0)
    return int(getattr(stats, name, 0) or 0)


def _opt_in_flags() -> dict[str, str]:
    """Map configured source keys to the CLI flag that would enable them."""
    flags = {key: _MARKETPLACES_FLAG for key in MARKETPLACE_SOURCE_KEYS}
    for key in LINKEDIN_OPT_IN_KEYS:
        flags[key] = _LINKEDIN_FLAG
    return flags


def _coverage_gap(rows) -> str | None:
    """One summarised gap for configured sources this pass could not run.

    Summarised by status (never enumerated one-per-source) and strictly about
    THIS RUN's coverage -- it never claims a source or fact is absent from the
    world.
    """
    counts: dict[str, int] = {}
    for row in rows or ():
        status = row.get("status")
        if status in _UNRUN_STATUSES:
            counts[status] = counts.get(status, 0) + 1
    if not counts:
        return None
    parts = ", ".join(f"{counts[s]} {s}" for s in _UNRUN_STATUSES if s in counts)
    return (
        f"sources that did not run this pass: {parts}; they are outside this "
        "run's coverage (opt in with the relevant flag or supply the account "
        "field and re-run)"
    )


def _first_party_doc_count(db, domain: str) -> int | None:
    """How many first-party documents ``needs`` could see this run.

    Delegates to the SAME resolver ``NeedsSource.local_harvest`` iterates
    (``_first_party_doc_ids``), so the number the dossier reports is the number
    the harvester actually saw -- never a second, drifting definition of
    "first-party". ``None`` when the count cannot be taken (no database handle,
    or the resolver raised): the gap then refuses to claim a count rather than
    guessing one.
    """
    if db is None:
        return None
    try:
        from src.sources.needs.collector import _first_party_doc_ids

        return len(_first_party_doc_ids(db, domain))
    except Exception:
        logger.exception("first-party document count failed for {}", domain)
        return None


def _jobs_summary(db, domain: str) -> dict | None:
    """Open-job counts for a domain from the jobs table.

    Open = ``closed_at`` is null. Never raises: a database problem degrades to
    ``None`` (the dossier renders ``jobs: not supplied``) rather than failing
    the package stage.
    """
    if db is None:
        return None
    try:
        rows = db.query(
            "SELECT department, country, closed_at FROM jobs WHERE domain = ?",
            (domain,),
        ) or []
    except Exception:
        logger.exception("jobs summary query failed for {}", domain)
        return None

    by_department: dict[str, int] = {}
    by_country: dict[str, int] = {}
    open_count = 0
    for row in rows:
        closed = row.get("closed_at") if hasattr(row, "get") else None
        if closed:
            continue
        open_count += 1
        dept = (row.get("department") if hasattr(row, "get") else None) or "unknown"
        country = (row.get("country") if hasattr(row, "get") else None) or "unknown"
        by_department[dept] = by_department.get(dept, 0) + 1
        by_country[country] = by_country.get(country, 0) + 1
    return {
        "open_count": open_count,
        "by_department": dict(sorted(by_department.items())),
        "by_country": dict(sorted(by_country.items())),
    }


def _account_count(orc) -> int | None:
    """Rows in the ``accounts`` table, or ``None`` when it cannot be read.

    Used to report how many accounts the global fanout seeded during a run
    (the measured Finding 6 defect: +60 accounts for one company). Never
    raises: a db problem degrades to ``None``.
    """
    db = getattr(orc, "db", None)
    if db is None:
        return None
    try:
        row = db.one("SELECT COUNT(*) AS n FROM accounts")
    except Exception:
        row = None
    if row is not None:
        try:
            return int(row["n"] if hasattr(row, "__getitem__") else row)
        except Exception:
            return None
    try:
        return len(db.query("SELECT domain FROM accounts"))
    except Exception:
        return None


def run_intel(
    target: str,
    *,
    name: str | None = None,
    market_profile_id: str | None = None,
    skip: tuple[str, ...] = (),
    force: bool = False,
    dry_run: bool = False,
    with_marketplaces: bool = False,
    with_linkedin_resolve: bool = False,
    include_fanout: bool = False,
    with_llm: bool = False,
    write: bool = True,
    max_signals: int | None = None,
    config=None,
    orch=None,
) -> dict:
    """Run the full intelligence flow for one domain.

    Returns ``{domain, created, stages, errors, coverage, gaps, dossier,
    paths}``. See the module docstring for the failure and dry-run contracts.

    Decide layer (Task 7): ``with_llm`` opts the run into the LLM
    plan/implement/decide sub-stages, which additionally require the resolved
    decide mode to be anything but "off" (config/decide.yaml or
    SIGNALS_DECIDE_MODE) and, in production, the API keys + data-exit
    consent. Off-by-default: without the flag the flow is byte-identical to
    the pre-layer coordinator. When the layer ran, the return dict gains a
    ``decide`` key (``{mode, degraded, rows}``).
    """
    domain = parse_target(target)
    skip = tuple(skip or ())

    # A per-invocation correlation id, minted once here and carried into the
    # dossier (and therefore into the package directory and manifest) by
    # ``build_dossier``. It correlates ARTIFACTS from this run only; it is not
    # a parent/child run-log id (see the module docstring).
    invocation_id = uuid.uuid4().hex[:8]

    cfg = config if config is not None else _load_config()
    orc = orch if orch is not None else _build_orchestrator(cfg)

    registry = orc.registry
    account = registry.get(domain)
    created = account is None

    if dry_run and account is None:
        raise ValueError(
            f"dry-run requires an existing account: {domain} is not in the "
            "registry (run without --dry-run to create it)"
        )

    if not dry_run:
        if account is None:
            account = Account(
                domain=domain,
                name=name or domain.split(".")[0].capitalize(),
            )
            registry.upsert(account, source="intel")
        elif name and account.name != name:
            account.name = name
            registry.upsert(account, source="intel")
        account = registry.get(domain) or account

    stages: dict[str, dict] = {}
    errors: dict[str, str] = {}
    gaps: list[str] = []
    outcomes: list = []
    coverage_rows: list[dict] = []
    dossier = None
    paths: dict = {}
    snapshot = None
    market_profile = None
    profile_id = _profile_id(market_profile_id, cfg)

    def record(stage: str, status: str, **info) -> None:
        info["status"] = status
        stages[stage] = info

    # -- decide layer setup (Task 7) ------------------------------------------
    # Off-by-default: the mode is ALWAYS resolved (the "not requested" gap can
    # only fire when the operator configured mode != off but skipped
    # --with-llm; in the shipped posture — config mode "off", no flag — every
    # branch below is dead and the run is byte-identical, which is the Goal
    # invariant this wiring resolves the plan's tension in favor of).
    decide_cfg = _load_decide_cfg(cfg)
    decide_mode = decide_policy.resolve_mode(decide_cfg)
    decide_requested = bool(with_llm and decide_mode != "off")
    # A dry run never spends: the layer stays "requested" (for honest skipped
    # records) but no decider is attached and no seam is called.
    layer_active = bool(decide_requested and not dry_run)
    ledger = DecideLedger() if decide_requested else None
    decider = None
    decide_degraded = False

    if not with_llm and decide_mode != "off":
        # The operator configured the layer but did not opt THIS run in: the
        # gap is the only side effect, and it cannot fire in the shipped
        # default posture (mode: "off").
        gaps.append("llm layer not requested (--with-llm); present as a gap, not a hole")
    elif with_llm and decide_mode == "off":
        gaps.append(
            "llm layer requested but decide mode is off "
            "(config/decide.yaml mode / SIGNALS_DECIDE_MODE); run stayed deterministic"
        )

    if decide_requested:
        # Run header: models and CONSENT PRESENCE only — key values and the
        # consent string itself are never logged.
        impl_cfg = decide_cfg.get("implementers") or {}
        consent_ok = os.environ.get("SIGNALS_DECIDE_DATA_EXIT") == DATA_EXIT_CONSENT
        logger.info(
            "decide layer active: mode={} decider_model={} planner_model={} "
            "bulk_model={} reasoning_model={} jev_credentials={} "
            "implementer_credentials={}",
            decide_mode,
            (decide_cfg.get("decider") or {}).get("model"),
            (decide_cfg.get("planner") or {}).get("model"),
            (impl_cfg.get("bulk") or {}).get("model"),
            (impl_cfg.get("reasoning") or {}).get("model"),
            bool(os.environ.get("TYPESAFE_API_KEY")) and consent_ok,
            bool(os.environ.get("OPENROUTER_API_KEY")) and consent_ok,
        )

    if layer_active:
        # run_intel is the ONLY call site allowed to attach a live decider
        # (decide/policy.py enforces it by stack inspection).
        try:
            decider = decide_policy.attach_decider(
                get_decider({**decide_cfg, "mode": decide_mode})
            )
        except Exception as exc:
            logger.exception("decide layer: decider attach failed for {}", domain)
            gaps.append(f"decide layer decider unavailable ({exc}); gates run not applicable")
            decider = NullDecider()

    # -- identity -----------------------------------------------------------
    if dry_run:
        record("identity", "skipped", note="dry-run: resolution is not performed")
    elif "identity" in skip:
        record("identity", "skipped", note="skipped by request")
    else:
        try:
            resolved = orc.resolve(
                domains=[domain],
                ats=True,
                cik=True,
                feeds=True,
                icp=True,
                appstore=True,
                bbb=True,
                g2=with_marketplaces,
                linkedin=with_linkedin_resolve,
            )
            # Re-read so fields filled by the resolvers (careers_url,
            # ats_vendor, cik, icp_fit) are visible to later stages.
            account = registry.get(domain) or account
            record("identity", "ran", resolved=resolved if isinstance(resolved, dict) else {})
        except Exception as exc:
            logger.exception("intel identity stage failed for {}", domain)
            errors["identity"] = str(exc)
            record("identity", "failed", reason=str(exc))
            gaps.append(f"identity stage failed: {exc}")
            account = registry.get(domain) or account

    # -- plan (decide layer sub-stage; AFTER identity, BEFORE collect) --------
    # After identity: the planner consumes the RESOLVED account —
    # sources_for_account filters on the requires fields (careers_url, cik,
    # feed_url) that the resolvers just filled. Before collect: the surviving
    # steps narrow the collect scope in enforce mode. Layer off means NO
    # trace: not even a skipped stage entry — the stage map stays
    # byte-identical to the pre-layer flow.
    plan_steps: list = []  # steps whose G1 action is "keep" (would-be in shadow)
    dropped_steps: list = []  # (step, drop reason) — enforce's deterministic fallback
    plan_reason = None
    if not decide_requested:
        pass
    elif dry_run:
        record("plan", "skipped", note="dry-run")
    elif "collect" in skip:
        record("plan", "skipped", note="collect skipped by request")
    else:
        try:
            adapters = enabled_sources(
                cfg,
                include_disabled=set(MARKETPLACE_SOURCE_KEYS) if with_marketplaces else None,
            )
            allowed = {a.key for a in sources_for_account(account, adapters)}
            sources_yaml = cfg.load_yaml("sources") or {}
            entries = sources_yaml.get("sources", sources_yaml) or {}
            knob_caps = {
                key: {
                    k: v
                    for k, v in (entry or {}).items()
                    if k.endswith("_max") and isinstance(v, int)
                }
                for key, entry in entries.items()
                if isinstance(entry, dict)
            }
            fanout_keys = {a.key for a in adapters if getattr(a, "fanout", False)}
            identity_state = _identity_state(domain, account, profile_id)
            identity_json = json.dumps(identity_state, sort_keys=True, default=str)
            snapshot_hash = hashlib.sha256(identity_json.encode("utf-8")).hexdigest()
            icp_excerpt = truncate(identity_json, _ICP_EXCERPT_CHARS)

            llm_plan, plan_reason = llm_planner.generate_plan(
                decide_cfg=decide_cfg,
                domain=domain,
                profile=profile_id,
                snapshot_hash=snapshot_hash,
                icp_excerpt=icp_excerpt,
                allowed_sources=allowed,
                knob_caps=knob_caps,
                fanout_keys=fanout_keys,
                include_fanout=include_fanout,
                dry_run=False,  # the dry-run path never reaches this branch
            )
            if llm_plan is None:
                gaps.append(
                    f"llm planner unavailable ({plan_reason}); deterministic source scope retained"
                )
            else:
                # G1: only a qualified step keeps its LLM override standing; a
                # dropped step is REMOVED from the override (narrow-only — a
                # gate verdict can never widen the scope) but its sources fall
                # back to deterministic collection, never silent removal. The
                # plan override itself is applied ONLY in enforce mode: in
                # shadow the steps are still judged (rows/artifacts show what
                # enforce would do) yet nothing binds (invariant 5).
                plan_gate_enabled = _gate_enabled(decide_cfg, "plan_qualification")
                for step in llm_plan.steps:
                    if not plan_gate_enabled:
                        break  # gate disabled by config: no verdict, nothing binds
                    action, decision = decide_gates.gate_plan_step(
                        step,
                        icp_excerpt,
                        decider,
                        _gate_cfg(decide_cfg, "plan_qualification"),
                        ledger,
                        invocation_id,
                        decide_mode,
                    )
                    if action == "keep":
                        plan_steps.append(step)
                    else:
                        dropped_steps.append((step, _g1_drop_reason(decision)))
            if plan_steps:
                plan_info: dict = {
                    "steps": len(plan_steps),
                    # v1 applies ONLY source narrowing: the validated
                    # per-source budget knobs are recorded but NOT enforced —
                    # the runner has no per-source knob override seam yet, so
                    # "apply per-source budget knobs at the runner level"
                    # stays on the verify-before-wire list.
                    "applied_sources": [
                        sid for step in plan_steps for sid in step.source_ids
                    ],
                    "knobs_applied": False,
                    "reason": plan_reason,
                }
                if decide_mode != "enforce":
                    # Mode consistency: the sources were NOT applied — the
                    # collect below keeps the deterministic full scope.
                    plan_info["note"] = "shadow: deterministic scope retained"
                record("plan", "ran", **plan_info)
            elif dropped_steps:
                record(
                    "plan",
                    "ran",
                    steps=0,
                    note="no qualified steps; deterministic collection retained for dropped steps",
                )
            else:
                record("plan", "ran", steps=0, note="no qualified steps; deterministic scope")
        except Exception as exc:
            logger.exception("intel plan sub-stage failed for {}", domain)
            errors["plan"] = str(exc)
            record("plan", "failed", reason=str(exc))
            gaps.append(f"plan sub-stage failed: {exc}")
            plan_steps = []
            dropped_steps = []

    # -- collect ------------------------------------------------------------
    include_disabled = set(MARKETPLACE_SOURCE_KEYS) if with_marketplaces else None
    # Finding 6: the three global fanout adapters are opt-in. Count the
    # accounts table around the PRIMARY pass so an included fanout can report
    # how many accounts it seeded.
    accounts_before = (
        _account_count(orc) if (include_fanout and not dry_run) else None
    )
    if "collect" in skip:
        record("collect", "skipped", note="skipped by request")
    else:
        try:
            # In ENFORCE mode, when G1-qualified plan steps survived, the
            # collect NARROWS to them (one call per step, plan order —
            # narrow-only, a verdict can never widen scope). A DROPPED step's
            # sources are NEVER silently removed (that would be worse than
            # mode:off): one final phase re-collects their union
            # deterministically, deduplicated against the surviving steps,
            # and each drop is gap-visible. In shadow — and whenever no plan
            # qualified — the single deterministic full-scope collect runs,
            # exactly as before.
            phases: list = []
            if decide_mode == "enforce" and (plan_steps or dropped_steps):
                phases = [list(step.source_ids) for step in plan_steps]
                planned: set = {sid for step in plan_steps for sid in step.source_ids}
                fallback: list[str] = []
                for step, reason in dropped_steps:
                    for sid in step.source_ids:
                        if sid not in planned:
                            fallback.append(sid)
                            planned.add(sid)
                    gaps.append(
                        f"G1 dropped plan step (sources: {', '.join(sorted(step.source_ids))}) "
                        f"after Jev verdict/error ({reason}); deterministic collection "
                        "retained for them"
                    )
                if fallback:
                    phases.append(fallback)
            if not phases:
                phases = [None]
            tasks_total = signals_total = sources_planned_total = 0
            for phase_sources in phases:
                stats = orc.collect(
                    sources=phase_sources,
                    domains=[domain],
                    force=force,
                    dry_run=dry_run,
                    limit=None,
                    include_disabled_sources=include_disabled,
                    local_context=None,
                    skip_fanout=not include_fanout,
                )
                outcomes.extend(_outcomes(stats))
                tasks_total += _stat(stats, "tasks")
                signals_total += _stat(stats, "signals_new")
                sources_planned_total += len(_outcomes(stats))
            if dry_run:
                record(
                    "collect",
                    "ran",
                    dry_run=True,
                    planned_tasks=tasks_total,
                    sources_planned=sources_planned_total,
                )
            else:
                record(
                    "collect",
                    "ran",
                    tasks=tasks_total,
                    signals_new=signals_total,
                )
        except Exception as exc:
            logger.exception("intel collect stage failed for {}", domain)
            errors["collect"] = str(exc)
            record("collect", "failed", reason=str(exc))
            gaps.append(f"collect stage failed: {exc}")

    # -- G2 posture audit (decide layer; informational only) -------------------
    # Shadow-only by construction: the verdict NEVER binds (gates.py). Dry runs
    # fetch nothing, so there is no fetch scope to audit.
    if (
        layer_active
        and decider is not None
        and _gate_cfg(decide_cfg, "posture_audit").get("enabled", True)
    ):
        try:
            ran_sources = sorted(
                {
                    str(row.get("source"))
                    for row in outcomes
                    if str(row.get("status") or "").startswith("ran")
                }
            )
            decide_gates.gate_posture_audit(
                "intel_collect",
                ", ".join(ran_sources) if ran_sources else "no sources ran",
                decider,
                _gate_cfg(decide_cfg, "posture_audit"),
                ledger,
                invocation_id,
                decide_mode,
            )
        except Exception as exc:
            logger.exception("decide layer: posture audit failed for {}", domain)
            gaps.append(f"posture audit failed: {exc}")

    # -- fanout honesty (Finding 6) ------------------------------------------
    # The dossier must say which posture this run took: the globals were
    # skipped (opt in next time) or ran and seeded N unrelated accounts.
    if include_fanout:
        accounts_after = _account_count(orc)
        if accounts_before is None or accounts_after is None:
            delta = None
        else:
            delta = accounts_after - accounts_before
        gaps.append(
            "fanout sources ran (sec_formd, federal_register, warn_notices) "
            f"- accounts created during collect: {delta if delta is not None else 'unknown'}"
        )
    else:
        gaps.append(
            "fanout sources skipped (sec_formd, federal_register, warn_notices) "
            "- pass --include-fanout"
        )

    # -- derive -------------------------------------------------------------
    # The market profile is loaded FIRST: it is the relevance vocabulary the
    # local harvesters need, and the loading decision (None on empty/unknown)
    # must be settled before the pass runs.
    if dry_run:
        record("derive", "skipped", note="dry-run: the local derived pass is not executed")
    elif "derive" in skip:
        record("derive", "skipped", note="skipped by request")
    else:
        try:
            market_profile = _load_profile(profile_id, gaps)
            derived = orc.collect(
                sources=list(LOCAL_DERIVED_SOURCES),
                domains=[domain],
                force=True,
                dry_run=False,
                limit=None,
                include_disabled_sources=None,
                local_context={
                    "market_profile": market_profile,
                    "market_profile_id": profile_id,
                },
            )
            outcomes.extend(_outcomes(derived))
            record(
                "derive",
                "ran",
                sources=list(LOCAL_DERIVED_SOURCES),
                signals_new=_stat(derived, "signals_new"),
            )
        except Exception as exc:
            logger.exception("intel derive stage failed for {}", domain)
            errors["derive"] = str(exc)
            record("derive", "failed", reason=str(exc))
            gaps.append(f"derive stage failed: {exc}")

    # -- why `needs` promoted nothing (Finding 9) ----------------------------
    # A missing/empty market profile makes ``needs`` return [] early: there is
    # no relevance vocabulary, so nothing can EVER be promoted. Without this
    # line the dossier shows ``market profile: -`` and a reader cannot tell
    # "no first-party evidence" from "no profile configured". A CONFIGURED
    # profile stays silent here: a profile that matches nothing is ordinary,
    # not a gap. This only REPORTS -- `needs` behaviour is untouched.
    if not dry_run and "derive" not in skip and market_profile is None:
        count = _first_party_doc_count(getattr(orc, "db", None), domain)
        if count is None:
            count_text = "first-party document count unavailable (no database handle in scope)"
        elif count == 0:
            count_text = "0 first-party documents in scope"
        else:
            count_text = f"{count} first-party documents were in scope"
        gaps.append(
            "needs promoted 0 signals: no market profile configured "
            "(config/markets.yaml ships an empty default) - "
            f"{count_text}; pass --market-profile <id> to enable promotion"
        )

    # -- implement (decide layer sub-stage; additive to derive) ----------------
    # Layer off means NO trace. The sub-stage reads ALREADY-STORED documents,
    # so it sits after the derive pass; its output is ADDITIVE — any failure
    # inside drops only the LLM content and leaves today's dossier.
    llm_fields: dict = {}
    if not decide_requested:
        pass
    elif dry_run:
        record("implement", "skipped", note="dry-run")
    else:
        docs_count = claims_count = promoted_count = 0
        try:
            if getattr(orc, "raw", None) is None:
                gaps.append("llm implementers unavailable (no raw store in scope)")
                record("implement", "skipped", note="no raw store in scope")
            elif not os.environ.get("OPENROUTER_API_KEY") or (
                os.environ.get("SIGNALS_DECIDE_DATA_EXIT") != DATA_EXIT_CONSENT
            ):
                # Credentials checked ONCE, before any spend — the bulk and
                # reasoning implementers share the key and the consent.
                gaps.append(
                    "llm implementers unavailable (missing OPENROUTER_API_KEY or "
                    "data-exit consent); deterministic derive output retained"
                )
                record("implement", "skipped", note="missing implementer credentials")
            else:
                impl = _implement_pass(
                    orc=orc,
                    account=account,
                    domain=domain,
                    decider=decider,
                    decide_cfg=decide_cfg,
                    ledger=ledger,
                    run_id=invocation_id,
                    mode=decide_mode,
                )
                for gap_line in impl.get("gaps", ()):
                    gaps.append(gap_line)
                llm_fields = impl.get("llm_fields") or {}
                if impl.get("degraded"):
                    # Whole-run token-budget degradation: from here on the
                    # layer is shadow-only, and the flag rides to the dossier
                    # and the manifest (invariant 3).
                    decide_mode = "shadow"
                    decide_degraded = True
                docs_count = int(impl.get("docs", 0))
                claims_count = int(impl.get("claims_accepted", 0))
                promoted_count = int(impl.get("promoted", 0))
                typed_count = int(impl.get("claims_typed", 0) or 0)
                reasons = impl.get("docs_excluded_reasons") or {}
                escalations = int(impl.get("five_fields_escalations", 0) or 0)
                quarantined = int(impl.get("five_fields_quarantined", 0) or 0)
                dates_extracted = int(impl.get("event_dates_extracted", 0) or 0)
                amounts_extracted = int(impl.get("amounts_extracted", 0) or 0)
                reranked_count = int(impl.get("docs_reranked", 0) or 0)
                record(
                    "implement",
                    "ran",
                    docs=docs_count,
                    claims_accepted=claims_count,
                    promoted=promoted_count,
                    llm_fields=bool(llm_fields),
                    docs_screened=int(impl.get("docs_screened", 0)),
                    docs_excluded=int(impl.get("docs_excluded", 0)),
                    **({"docs_excluded_reasons": reasons} if reasons else {}),
                    **({"claims_typed": typed_count} if typed_count else {}),
                    **({"five_fields_escalations": escalations} if escalations else {}),
                    **({"five_fields_quarantined": quarantined} if quarantined else {}),
                    **({"event_dates_extracted": dates_extracted} if dates_extracted else {}),
                    **({"amounts_extracted": amounts_extracted} if amounts_extracted else {}),
                    **({"docs_reranked": reranked_count} if reranked_count else {}),
                )
        except Exception as exc:
            logger.exception("intel implement sub-stage failed for {}", domain)
            errors["implement"] = str(exc)
            record("implement", "failed", reason=str(exc))
            gaps.append(f"implement sub-stage failed: {exc}")

    # -- score --------------------------------------------------------------
    if dry_run:
        record("score", "skipped", note="dry-run: scoring is not performed")
    elif "score" in skip:
        record("score", "skipped", note="skipped by request")
    else:
        try:
            scored = orc.score(domains=[domain], return_snapshots=True)
            snapshots = scored.get("snapshots") if isinstance(scored, dict) else None
            snapshot = (snapshots or {}).get(domain)
            if snapshot is None:
                # A missing snapshot mapping is a stage FAILURE, never an
                # empty dossier.
                msg = f"score returned no snapshot for {domain}"
                errors["score"] = msg
                record("score", "failed", reason=msg)
                gaps.append(f"score stage failed: {msg}")
            else:
                record(
                    "score",
                    "ran",
                    scored=(scored.get("scored") if isinstance(scored, dict) else None),
                )
        except Exception as exc:
            logger.exception("intel score stage failed for {}", domain)
            errors["score"] = str(exc)
            record("score", "failed", reason=str(exc))
            gaps.append(f"score stage failed: {exc}")

    # -- decide degradation visibility (invariant 3) ---------------------------
    # The token pre-flight already flags whole-run degradation. Enforce-mode
    # Jev degradation must be artifact-visible TOO: any ledger row carrying
    # an error (a gate's on_error fallback fired) or a tripped Jev breaker
    # sets decide_degraded at package time, so the flag rides to the dossier,
    # the manifest and the return dict and a gap names the cause. Shadow runs
    # record the same rows but never flag (nothing bound).
    if (
        decide_requested
        and ledger is not None
        and not decide_degraded
        and decide_mode == "enforce"
    ):
        error_rows = sum(1 for row in ledger.rows if row.get("error") is not None)
        breaker_open = bool(
            isinstance(decider, LiveDecider) and getattr(decider, "breaker_open", False)
        )
        if error_rows or breaker_open:
            decide_degraded = True
            causes = []
            if error_rows:
                causes.append(f"{error_rows} Jev errors")
            if breaker_open:
                causes.append("breaker open")
            gaps.append(
                f"decide layer degraded during run: {' / '.join(causes)}; "
                "on_error fallbacks applied; artifact flagged decide_degraded"
            )

    # -- package ------------------------------------------------------------
    if dry_run:
        record("package", "skipped", note="dry-run: no package is built or written")
    elif "package" in skip:
        record("package", "skipped", note="skipped by request")
    elif snapshot is None:
        record(
            "package",
            "skipped",
            note="no snapshot available for this domain; package not built",
        )
    else:
        try:
            coverage_rows = build_coverage(
                config=cfg,
                account=account,
                outcomes=outcomes,
                opt_in_flags=_opt_in_flags(),
                requested=None,
            )
            coverage_gap = _coverage_gap(coverage_rows)
            if coverage_gap:
                gaps.append(coverage_gap)
            jobs_summary = _jobs_summary(getattr(orc, "db", None), domain)
            # Decide-layer provenance is passed ONLY when the layer was
            # requested — the off path hands the builder exactly the pre-layer
            # kwargs (the existing test doubles pin the old signature).
            dossier_kwargs: dict = {}
            if decide_requested and ledger is not None:
                gates_seen: list[str] = []
                for row in ledger.rows:
                    gate = str(row.get("gate"))
                    if gate not in gates_seen:
                        gates_seen.append(gate)
                if gates_seen:
                    dossier_kwargs["decide_records"] = [
                        {"gate": gate, "detail": ledger.aggregate(gate)} for gate in gates_seen
                    ]
                dossier_kwargs["decide_meta"] = {
                    "mode": decide_mode,
                    "degraded": decide_degraded,
                    "shadow_appendix": _shadow_appendix(ledger.rows),
                }
            if llm_fields:
                # The five G4-gated LLM dossier fields reach the dossier only
                # when the implement sub-stage produced them — the off path
                # (empty llm_fields) adds no key (byte-identity).
                dossier_kwargs["llm_fields"] = llm_fields
            dossier = build_dossier(
                snapshot,
                coverage=coverage_rows,
                market_profile=market_profile,
                market_profile_id=profile_id,
                jobs_summary=jobs_summary,
                gaps=gaps,
                max_signals=max_signals,
                invocation_id=invocation_id,
                **dossier_kwargs,
            )
            if write:
                if decide_requested and ledger is not None:
                    paths = write_intel_package(
                        dossier,
                        out_dir=cfg.storage.dossiers_dir,
                        decide_rows=list(ledger.rows),
                    )
                else:
                    paths = write_intel_package(dossier, out_dir=cfg.storage.dossiers_dir)
            record(
                "package",
                "ran",
                written=bool(write),
                coverage_rows=len(coverage_rows),
            )
        except Exception as exc:
            logger.exception("intel package stage failed for {}", domain)
            errors["package"] = str(exc)
            record("package", "failed", reason=str(exc))
            gaps.append(f"package stage failed: {exc}")

    result = {
        "domain": domain,
        "created": created,
        "stages": stages,
        "errors": errors,
        "coverage": coverage_rows,
        "gaps": gaps,
        "dossier": dossier,
        "paths": paths or {},
    }
    if decide_requested:
        # The layer's own telemetry, ONLY when it was requested — the off-path
        # return dict is byte-identical to the pre-layer coordinator.
        result["decide"] = {
            "mode": decide_mode,
            "degraded": decide_degraded,
            "rows": len(ledger.rows) if ledger is not None else 0,
        }
    return result