"""Budgeted claim backfill replay (decide-lab Task 2 Step 2).

G4 ledger rows recorded before ``lab.persist_claims`` shipped carry a
``state_hash`` but not the claim batch, so their states are not replayable.
:func:`backfill` finds those corpus docs (reusing :mod:`src.lab.corpus` —
the same state-hash document join) and re-runs the bulk implementer on the
FULL stored body to regenerate the claims, caching them to
``<out_dir>/<doc_id>.json`` — cache-forever: the cache IS the state
(replayed claims differ from the originals by sampling temperature; the
plan's replay-drift mitigation is "cache once, never mix cached and fresh
within one comparison").

The replay uses the CURRENT extraction prompt
(:func:`src.llm.implement.build_extraction_messages`, default deep route) —
which since the G4-wall fix wave FORBIDS envelope/meta claims — so
backfilled claims follow the new shape. That is the point: the lab tunes
against the prompt that ships, not the one that produced the historic
hashes.

Spend discipline (the plan's "no unbounded spend" invariant):
- ``max_docs`` caps docs per invocation; remaining candidates are deferred
  to later invocations (never silently dropped from later runs).
- ``budget_calls`` is a simple call counter, checked BEFORE each call —
  never exceeded mid-flight. Cache hits cost nothing and are still served
  when the budget is spent.
- Model/base_url come from config/decide.yaml ``implementers.bulk``; the
  api key comes from the env (:data:`src.llm.implement.API_KEY_ENV`); the
  same data-exit consent string gates the egress as the intel pipeline.

Per-doc honesty: nothing raises past a document — an unreadable/corrupt
body is skipped, a failed or raising implementer call counts as an error
and is NOT cached (retryable next invocation), and the call seam itself
never raises (``call_implementer``'s None contract).

Stats (exact keys): ``considered`` (candidates examined this invocation =
the first ``max_docs`` backlog docs in corpus order; each gets exactly one
disposition), ``backfilled`` (fresh call succeeded, cache written — a
well-formed empty ``{"claims": []}`` counts), ``cached`` (cache hit, zero
spend), ``skipped`` (unreadable body, or the call budget was spent),
``errors`` (call failed/raised or the cache write failed).
``considered == backfilled + cached + skipped + errors`` always holds.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from src.core.config import Config
from src.decide.jev import DATA_EXIT_CONSENT
from src.lab import corpus
from src.llm import implement as llm_implement

DEFAULT_OUT_DIR = "data/lab/claims"
DEFAULT_DOSSIERS_DIR = "data/dossiers"

STATS_KEYS: tuple[str, ...] = ("considered", "backfilled", "cached", "skipped", "errors")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _decode_body(body: object) -> str:
    if isinstance(body, (bytes, bytearray)):
        return body.decode("utf-8", "replace")
    return str(body or "")


def _bulk_config(decide_cfg: dict) -> tuple[str, str, float]:
    """(model, base_url, timeout_s) from decide.yaml's ``implementers.bulk``
    — the exact wiring run_intel uses (timeout mirrors its default)."""
    bulk = (decide_cfg.get("implementers") or {}).get("bulk") or {}
    model = str(bulk.get("model") or "")
    base_url = str(bulk.get("base_url") or "")
    try:
        timeout_s = float(bulk.get("timeout_s", llm_implement.DEFAULT_TIMEOUT_S))
    except (TypeError, ValueError):
        timeout_s = llm_implement.DEFAULT_TIMEOUT_S
    return model, base_url, timeout_s


def _preflight() -> tuple[str, str, float, str]:
    """Resolve the egress posture BEFORE any work or spend: config, key,
    consent. A missing piece is a caller misconfiguration, not a per-doc
    failure — ValueError, no partial cache writes, no calls."""
    model, base_url, timeout_s = _bulk_config(Config().load_yaml("decide") or {})
    api_key = os.environ.get(llm_implement.API_KEY_ENV) or ""
    missing = [
        name
        for name, value in (
            ("implementers.bulk.model", model),
            ("implementers.bulk.base_url", base_url),
            (llm_implement.API_KEY_ENV, api_key),
        )
        if not value
    ]
    if os.environ.get("SIGNALS_DECIDE_DATA_EXIT") != DATA_EXIT_CONSENT:
        missing.append(f"SIGNALS_DECIDE_DATA_EXIT ({DATA_EXIT_CONSENT})")
    if missing:
        raise ValueError(
            "lab backfill preflight failed, nothing was called or cached; missing: "
            + ", ".join(missing)
        )
    return model, base_url, timeout_s, api_key


def _candidates(rows: list[dict]) -> list[str]:
    """Distinct doc_ids needing claims, in corpus order (first-seen across
    the sorted packages, rows in file order).

    A doc HAS claims when any citation row persisted them (the claim
    entries' doc_ids are that row's document link). A doc is a CANDIDATE
    when a joined document_gate row names it (unjoined/bogus-hash rows
    carry no doc_id and are invisible) and no persisted claims cite it —
    pre-persistence runs and docs the citation gate never judged alike;
    the gate row's outcome is not a filter (an excluded doc is still a
    corpus doc whose claims are missing, and a candidate for measuring
    what the gate passed over)."""
    have_claims: set[str] = set()
    ordered: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if row.get("row_type") != "decision":
            continue
        if row.get("gate") == "citation_soundness":
            claims = row.get("claims")
            if isinstance(claims, list):
                for claim in claims:
                    if isinstance(claim, dict) and isinstance(claim.get("doc_id"), str):
                        have_claims.add(claim["doc_id"])
        elif row.get("gate") == "document_gate" and row.get("doc_joined") and row.get("doc_id"):
            doc_id = str(row["doc_id"])
            if doc_id not in seen:
                seen.add(doc_id)
                ordered.append(doc_id)
    return [doc_id for doc_id in ordered if doc_id not in have_claims]


def _cache_claims(claims: list) -> list[dict]:
    """Claims in the persisted-claims payload shape (``lab.persist_claims``
    ledger rows): ``[{"text", "quote_span", "doc_id"}, ...]``."""
    return [
        {"text": c.text, "quote_span": c.quote_span, "doc_id": c.doc_id} for c in claims
    ]


def backfill(
    db,
    raw_store,
    domains: list[str] | None = None,
    *,
    max_docs: int,
    budget_calls: int,
    out_dir: str | Path = DEFAULT_OUT_DIR,
    dossiers_dir: str | Path = DEFAULT_DOSSIERS_DIR,
) -> dict:
    """Regenerate claims for corpus docs whose claims are missing; cache them.

    ``db``/``raw_store`` are the live stores; ``domains`` scopes the corpus
    scan (None = every package); ``dossiers_dir``/``raw_store.raw_dir`` feed
    :func:`src.lab.corpus.extract`. Returns the stats dict (exact keys
    :data:`STATS_KEYS`; see module docstring for the disposition rules).
    """
    model, base_url, timeout_s, api_key = _preflight()
    stats = {key: 0 for key in STATS_KEYS}

    rows = corpus.extract(
        db, dossiers_dir, domains=domains, raw_dir=getattr(raw_store, "raw_dir", "data/raw")
    )
    backlog = _candidates(rows)

    cache_dir = Path(out_dir)
    n_docs = max(0, int(max_docs))
    n_budget = max(0, int(budget_calls))
    calls_made = 0

    for doc_id in backlog[:n_docs]:
        stats["considered"] += 1
        cache_path = cache_dir / f"{doc_id}.json"
        if cache_path.exists():  # cache-forever: never revalidated, never rewritten
            stats["cached"] += 1
            continue

        # The corpus join carries only the 9600-char gate-state head; the
        # implementer gets the FULL stored body (unreadable => skipped).
        try:
            full = raw_store.get(doc_id)
        except Exception:
            full = None
        text = _decode_body(getattr(full, "body", None) if full is not None else None)
        if not text.strip():
            stats["skipped"] += 1
            continue

        if calls_made >= n_budget:  # checked BEFORE the call — never exceeded mid-flight
            stats["skipped"] += 1
            continue
        calls_made += 1

        try:
            raw = llm_implement.call_implementer(
                llm_implement.build_extraction_messages(doc_id, text),
                model=model,
                base_url=base_url,
                api_key=api_key,
                timeout_s=timeout_s,
            )
        except Exception:  # the seam never raises; a double might — never raise past a doc
            stats["errors"] += 1
            continue
        if raw is None:  # failed call: NOT cached, so a later invocation can retry
            stats["errors"] += 1
            continue

        payload = {
            "doc_id": doc_id,
            "model": model,
            "generated_at": _now(),
            "claims": _cache_claims(llm_implement.parse_claims(raw, batch_doc_id=doc_id)),
        }
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        except OSError:
            stats["errors"] += 1
            continue
        stats["backfilled"] += 1

    logger.info(
        "lab backfill: considered={} backfilled={} cached={} skipped={} errors={} calls={}",
        stats["considered"], stats["backfilled"], stats["cached"],
        stats["skipped"], stats["errors"], calls_made,
    )
    return stats
