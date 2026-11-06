"""Implementer tier probe (the need-density probe's follow-up):
does the promotion-noul ceiling (recorded max 0.34 vs the 0.70 floor) move
with a STRONGER implementer model on the SAME frozen documents, or is the
input prose the binding constraint?

Design (2026-11-06): BOTH arms run FRESH through the real pipeline seam —
call_implementer -> parse_claims -> screen_and_gate_claims ->
gate_need_promotion — over identical documents with an identical decider
and identical shipped gate config. The implementer model name is the only
variable. The recorded claims are reference-only: the fresh-vs-recorded
instability measured that day (abnormal: 46 fresh accepts vs 1 recorded
survivor at the same shipped threshold) makes fresh-vs-fresh the only
clean comparison.

The G5 gate is profile-blind (its question is "genuine buying intent?",
state = need + excerpt only), so the nouls measure generic buying-intent
strength; the corpus's vendor domains are deliberate non-prospects whose
homepages are structurally need-free — the verdict reads primarily on the
DTC arm.
"""

from __future__ import annotations

import json
from pathlib import Path

# Domains grouped by corpus role: vendor-software (their own marketing
# prose — reference, not prospects) vs DTC/prospect (the seller's ICP).
# Sorted prefixes keep the selection deterministic.
VENDOR_DOMAINS = frozenset({
    "abnormal.ai", "amigo.ai", "cority.com", "cyera.com", "cynomi.com",
    "databricks.com", "darktrace.com", "decagon.ai", "deepinfra.com",
    "elevenlabs.io", "glow.security", "helcim.com", "opositiv.com",
    "ramp.com", "rillet.com", "riverside.com", "snowflake.com",
})

_FLOOR = 0.70
_BAND_RELEASED_LOW = 0.45


def stratify_domains(domains: list[str], n_vendor: int = 2, n_dtc: int = 6) -> list[str]:
    """Deterministic DTC-weighted selection (sorted, capped per group).

    Default 6 DTC + 2 vendor: the vendor arm is the by-construction control
    (their homepages carry ~0.2% need-language per the need-density probe);
    the spend concentrates on the arm that can move."""
    vend = sorted(d for d in domains if d in VENDOR_DOMAINS)[:n_vendor]
    dtc = sorted(d for d in domains if d not in VENDOR_DOMAINS)[:n_dtc]
    return sorted(vend + dtc)


def _pctl(sorted_vals: list[float], q: float) -> float | None:
    if not sorted_vals:
        return None
    i = min(len(sorted_vals) - 1, max(0, round(q * (len(sorted_vals) - 1))))
    return sorted_vals[i]


def summarize_nouls(nouls: list[float]) -> dict:
    """The distribution the verdict reads: max, p50/p90, and the counts at
    the decision-relevant bands (floor 0.70; the released [0.45, 0.70)
    screen band; 0.45 overall). Zero-denominator/empty = honest None."""
    vs = sorted(v for v in nouls if isinstance(v, (int, float)))
    return {
        "n": len(vs),
        "max": vs[-1] if vs else None,
        "p50": _pctl(vs, 0.50),
        "p90": _pctl(vs, 0.90),
        "ge_floor": sum(1 for v in vs if v >= _FLOOR),
        "band_released": sum(1 for v in vs if _BAND_RELEASED_LOW <= v < _FLOOR),
        "ge_045": sum(1 for v in vs if v >= _BAND_RELEASED_LOW),
    }


def estimate_cost(domains: int, arms: int, cost_per_domain_usd: float = 0.33) -> float:
    """Pre-flight implementer spend estimate; the observed rate is per
    DOMAIN at arm A (docs already priced in) — scales linearly in domains
    and arms. Decider calls (TYPESAFE) bill separately and are pennies at
    these volumes."""
    return round(domains * cost_per_domain_usd * arms, 2)


def collect_docs(rows: list[dict], domains: list[str], per_domain: int = 12) -> list[dict]:
    """The frozen corpus's included, doc-joined document_gate rows, capped
    per domain (deterministic order) — the SAME documents the recorded
    claims came from."""
    by_dom: dict[str, list[dict]] = {}
    for r in rows:
        if (r.get("gate") == "document_gate" and r.get("doc_joined")
                and r.get("outcome") == "included"
                and str(r.get("domain") or "") in domains):
            by_dom.setdefault(str(r["domain"]), []).append(r)
    out: list[dict] = []
    for d in sorted(by_dom):
        for r in by_dom[d][:per_domain]:
            text = r.get("text")
            if isinstance(text, str) and text:
                out.append({"doc_id": str(r["doc_id"]), "text": text, "domain": d})
    return out


def run_arm(arm: str, model: str, docs: list[dict], decider, *,
            gates_cfg: dict, out_path: Path,
            bulk_base_url: str = "https://openrouter.ai/api/v1",
            api_key: str | None = None, timeout_s: float = 120.0) -> list[dict]:
    """One arm = the real pipeline seam per doc, serial (the shared
    llm_implement contract). Imports at call time (the lab's isolation
    guard stays one-directional). Writes one JSONL row per citation-accepted
    claim with its promotion noul; returns the same rows."""
    from src.decide.gates import DecideLedger, claim_excerpt
    from src.llm import implement as llm_implement

    citation_cfg = gates_cfg.get("citation_soundness") or {}
    screen_cfg = gates_cfg.get("output_screen") or {}
    promotion_cfg = gates_cfg.get("need_promotion") or {}
    ledger = DecideLedger()  # probe-local, in-memory
    run_id = f"tierprobe-{arm}"
    rows: list[dict] = []

    for spec in docs:
        raw = llm_implement.call_implementer(
            llm_implement.build_extraction_messages(spec["doc_id"], spec["text"]),
            model=model, base_url=bulk_base_url, api_key=api_key,
            timeout_s=timeout_s,
        )
        claims = llm_implement.parse_claims(raw, batch_doc_id=spec["doc_id"])
        pairs = llm_implement.screen_and_gate_claims(
            claims, batch_doc_id=spec["doc_id"], doc_text=spec["text"],
            decider=decider, gates_cfg=citation_cfg, ledger=ledger,
            run_id=run_id, mode="enforce", screen_cfg=screen_cfg,
            persist_claims=False,
        )
        accepted = [c for c, _d in pairs]
        for claim in accepted:
            excerpt = claim_excerpt(spec["text"], claim)
            promote, noul, _d = decide_promotion(
                decider, promotion_cfg, ledger, run_id,
                claim, excerpt,
            )
            rows.append({
                "arm": arm, "model": model, "domain": spec["domain"],
                "doc_id": spec["doc_id"], "claim": claim.text,
                "quote_span": claim.quote_span,
                "citation_outcome": "accepted", "promotion_noul": noul,
                "promoted": bool(promote),
            })
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, default=str) + "\n")
    return rows


def decide_promotion(decider, promotion_cfg: dict, ledger, run_id: str,
                     claim, excerpt: str | None):
    """The pipeline's G5 call, verbatim shape (intel.py:954-963): judge the
    claim against the excerpt anchored on its VERIFIED quote span."""
    from src.decide.gates import gate_need_promotion

    return gate_need_promotion(
        claim.text, excerpt, False, decider, promotion_cfg,
        ledger, run_id, "enforce", quote_span=claim.quote_span,
    )


def compare_arms(a: dict, b: dict) -> dict:
    """The pre-registered verdict rule (written BEFORE the run, per the
    plan): MODEL-TIER if arm B promotes at all or its p90 moved >= 0.15;
    PROSE-BOUND if arm B's max moved <= 0.10 with zero promotions;
    INCONCLUSIVE otherwise (re-run with more docs before deciding)."""
    moved_p90 = (b.get("p90") or 0) - (a.get("p90") or 0)
    moved_max = (b.get("max") or 0) - (a.get("max") or 0)
    if b.get("ge_floor", 0) >= 1 or moved_p90 >= 0.15:
        verdict = "MODEL-TIER ceiling broken — the careers harvest rides the stronger implementer"
    elif moved_max <= 0.10 and b.get("ge_floor", 0) == 0:
        verdict = "PROSE-BOUND — the ceiling is the input prose; the careers harvest is THE fix, keep the flash implementer"
    else:
        verdict = "INCONCLUSIVE — b moved without promotions; re-run with more docs before deciding"
    return {"verdict": verdict, "moved_p90": round(moved_p90, 3),
            "moved_max": round(moved_max, 3), "a": a, "b": b}
