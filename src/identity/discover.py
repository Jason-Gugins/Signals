"""Compose the four keyless name->domain resolvers into one waterfall.

Plan T4 wiring for the Clay "find the company" clone. ALL cross-stage logic
lives here — never in Orchestrator.resolve (which stays one flag block per
already-known-domain stage) and never inside the stage resolvers themselves
(each module does not self-dispatch and knows nothing about its siblings).

Stage order and first-hit early exit:
1. Wikidata P856 (wikidata_ids.WikidataDomainResolver)
2. Wikipedia extlinks (wikipedia_ids.WikipediaExtlinksResolver)
3. GKG (gkg_ids.GkgDomainResolver; EKG default backend, credential-gated —
   "unconfigured" without creds is a by-design no-op, recorded as such)
4. DDG SERP (ddg_ids.DdgSerpResolver) — ONLY when ddg_enabled: the fragile,
   robots-exception stage runs resolve-time only and must never run unless
   asked (opt-in --ddg on `sweep --discover`).

Auto-accept (never-guess contract): the waterfall resolves on a single
stage's "resolved" first hit, OR on 2-source apex agreement — no stage
resolved individually but >=2 stages surfaced the SAME root domain as a
candidate (e.g. Wikidata P856 == GKG url). The agreeing stages are recorded
under "agreement". Everything else stays ambiguous/no_match; the wiring
layer (Orchestrator.discover) owns persistence into identity_candidates.

Ranking of the merged candidate list is display-only. Candidates are never
fabricated: the merged list is exactly the union of what stages returned,
deduped by (stage, domain-or-url). One stage erroring never aborts the
waterfall (recorded as {"status": "error", ...}, remaining stages run).
"""

from __future__ import annotations

import json
from typing import Callable, Optional

from loguru import logger

from src.identity.domains import root_domain

# Display/priority order: merged-candidate ranking tie-break (score desc,
# then stage priority) and agreement tie-break (most stages, then earliest
# stage in this order).
STAGE_PRIORITY: tuple[str, ...] = ("wikidata", "wikipedia", "gkg", "ddg")


# -- stage runners -------------------------------------------------------------
# Module-level so tests (and future callers) can patch one stage without
# touching the others. Imports are deferred to call time like the resolvers.
def _run_wikidata(name: str, fetcher) -> dict:
    from src.identity.wikidata_ids import WikidataDomainResolver

    return WikidataDomainResolver(fetcher, None).discover(name)


def _run_wikipedia(name: str, fetcher) -> dict:
    from src.identity.wikipedia_ids import WikipediaExtlinksResolver

    return WikipediaExtlinksResolver(fetcher, None).discover(name)


def _run_gkg(name: str, gkg_backend: str) -> dict:
    from src.identity.gkg_client import KnowledgeGraphClient
    from src.identity.gkg_ids import GkgDomainResolver

    client = KnowledgeGraphClient(backend=gkg_backend)
    return GkgDomainResolver(client=client).discover(name)


def _run_ddg(name: str, ddg_fetcher, clock, sleep) -> dict:
    from src.identity.ddg_ids import DdgSerpResolver

    kwargs: dict = {}
    if clock is not None:
        kwargs["clock"] = clock
    if sleep is not None:
        kwargs["sleep"] = sleep
    return DdgSerpResolver(fetcher=ddg_fetcher, **kwargs).discover(name)


def _safe_stage(stage: str, name: str, run: Callable[[], dict]) -> dict:
    """One stage, isolated: an exception is recorded, never aborts the waterfall."""
    try:
        return run()
    except Exception as exc:
        logger.warning("discover: {} stage failed for {!r}: {}", stage, name, exc)
        return {"status": "error", "domain": None, "candidates": [], "error": str(exc)}


def _candidate_key(stage: str, cand: dict) -> tuple:
    """Dedup key: (stage, domain-or-url); full-dict fallback only for the
    rare candidates with neither (e.g. domain-less Wikidata survivors) so
    distinct entities are never collapsed and nothing is fabricated."""
    identity = cand.get("domain") or cand.get("url")
    if identity:
        return (stage, identity)
    return (stage, json.dumps(cand, sort_keys=True, default=str))


def _merge_candidates(stages: dict[str, dict]) -> list[dict]:
    """Union of all stage candidates (pure).

    Each candidate is annotated with "stage": <name>, deduped by
    (stage, domain-or-url), ranked by (score desc, stage priority
    wikidata>wikipedia>gkg>ddg; stable within a stage).
    """
    merged: list[dict] = []
    seen: set[tuple] = set()
    for stage in STAGE_PRIORITY:
        result = stages.get(stage) or {}
        for cand in result.get("candidates") or []:
            if not isinstance(cand, dict):
                continue
            key = _candidate_key(stage, cand)
            if key in seen:
                continue
            seen.add(key)
            item = dict(cand)
            item["stage"] = stage
            merged.append(item)
    merged.sort(
        key=lambda c: (
            -int(c.get("score") or 0),
            STAGE_PRIORITY.index(c["stage"]) if c.get("stage") in STAGE_PRIORITY else len(STAGE_PRIORITY),
        )
    )
    return merged


def _apex_agreement(stages: dict[str, dict]) -> Optional[tuple[str, list[str]]]:
    """2-source apex agreement over stage candidates (pure).

    A domain qualifies when >=2 DISTINCT stages surfaced it (each stage
    votes at most once per root domain). Winner: the domain agreed by the
    most stages, tie-broken by earliest stage in STAGE_PRIORITY. Returns
    (domain, [agreeing stages in priority order]) or None.
    """
    votes: dict[str, set[str]] = {}
    for stage in STAGE_PRIORITY:
        result = stages.get(stage) or {}
        for cand in result.get("candidates") or []:
            domain = root_domain(cand.get("domain") if isinstance(cand, dict) else None)
            if domain:
                votes.setdefault(domain, set()).add(stage)
    best: Optional[tuple[tuple[int, int], str, list[str]]] = None
    for domain, agreeing in votes.items():
        if len(agreeing) < 2:
            continue
        ordered = sorted(agreeing, key=STAGE_PRIORITY.index)
        rank = (-len(ordered), min(STAGE_PRIORITY.index(s) for s in ordered))
        if best is None or rank < best[0]:
            best = (rank, domain, ordered)
    if best is None:
        return None
    return best[1], best[2]


def discover_waterfall(
    name: str,
    fetcher,
    *,
    gkg_backend: str = "ekg",
    ddg_enabled: bool = False,
    ddg_fetcher=None,
    clock: Optional[Callable[[], float]] = None,
    sleep: Optional[Callable[[float], None]] = None,
) -> dict:
    """Run the discovery waterfall for one company name.

    Returns {"name", "status": resolved|ambiguous|no_match, "domain",
    "candidates": [...merged, ranked...], "stages": {per-stage results}},
    plus "agreement": [stages] when the 2-source apex rule fired. The
    "ddg" stage key appears only when ddg_enabled; stages skipped by a
    first-hit early exit are recorded as {"status": "skipped"}. Never
    raises for stage failures (isolated per stage); never fabricates.
    """
    term = (name or "").strip()
    plan: list[tuple[str, Callable[[], dict]]] = [
        ("wikidata", lambda: _run_wikidata(term, fetcher)),
        ("wikipedia", lambda: _run_wikipedia(term, fetcher)),
        ("gkg", lambda: _run_gkg(term, gkg_backend)),
    ]
    if ddg_enabled:
        plan.append(("ddg", lambda: _run_ddg(term, ddg_fetcher, clock, sleep)))

    stages: dict[str, dict] = {}
    hit: Optional[tuple[str, dict]] = None
    for stage, run in plan:
        result = _safe_stage(stage, term, run)
        stages[stage] = result
        if result.get("status") == "resolved":
            hit = (stage, result)
            break
    # First-hit early exit: stages not reached are recorded, never run.
    for stage, _run in plan:
        stages.setdefault(stage, {"status": "skipped"})

    merged = _merge_candidates(stages)
    agreement: Optional[list[str]] = None
    if hit is not None:
        status, domain = "resolved", hit[1].get("domain")
    else:
        agreed = _apex_agreement(stages)
        if agreed is not None:
            domain, agreement = agreed
            status = "resolved"
        else:
            domain = None
            status = "ambiguous" if merged else "no_match"

    out: dict = {
        "name": name,
        "status": status,
        "domain": domain,
        "candidates": merged,
        "stages": stages,
    }
    if agreement:
        out["agreement"] = agreement
    return out
