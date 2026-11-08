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
import sys
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Annotation-only: tierprobe's src.decide imports stay call-time (the
    # lab isolation posture) — these never execute at runtime.
    from src.decide.gates import DecideLedger
    from src.decide.jev import Decider
    from src.decide.shapes import Claim, Decision

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


def run_arm(arm: str, model: str, docs: list[dict], decider: Decider, *,
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
        try:
            raw = llm_implement.call_implementer(
                llm_implement.build_extraction_messages(spec["doc_id"], spec["text"]),
                model=model, base_url=bulk_base_url, api_key=api_key or "",
                timeout_s=timeout_s,
            )
            claims = llm_implement.parse_claims(raw, batch_doc_id=spec["doc_id"])
            pairs = llm_implement.screen_and_gate_claims(
                claims, batch_doc_id=spec["doc_id"], doc_text=spec["text"],
                decider=decider, gates_cfg=citation_cfg, ledger=ledger,
                run_id=run_id, mode="enforce", screen_cfg=screen_cfg,
                persist_claims=False,
            )
        except Exception as exc:
            # one doc's failure (rate limit, 402 credits, timeout) must not
            # lose the arm's other rows — skip and continue, counted.
            print(f"  [{arm}] doc {spec['doc_id'][:12]}… failed: "
                  f"{type(exc).__name__}: {exc}")
            continue
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


def decide_promotion(
    decider: Decider, promotion_cfg: dict, ledger: DecideLedger, run_id: str,
    claim: Claim, excerpt: str,
) -> tuple[bool, float | None, Decision]:
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


MODEL_A = "deepseek/deepseek-v4.1-flash"
MODEL_B = "z-ai/glm-5.3"
MODEL_C = "anthropic/claude-sonnet-5.5"
COST_PER_DOMAIN_USD = 0.33


# raw_dir defaults to corpus.extract's "data/raw" (never None — the old
# None default was a latent TypeError into corpus.extract(raw_dir: str | Path)).
def extract_rows(db_path: Path, dossiers: Path,
                 raw_dir: str | Path = "data/raw",
                 domains: list[str] | None = None) -> list[dict]:
    """The corpus-extraction seam (monkeypatched by tests). Opens the
    signals DB READ-ONLY via the lab's stand-in — never migrates."""
    from src.lab import corpus as corpus_mod
    from src.lab.__main__ import _ReadOnlyDB

    db = _ReadOnlyDB(Path(db_path))
    try:
        return corpus_mod.extract(db, Path(dossiers), domains=domains,
                                  raw_dir=raw_dir)
    finally:
        close = getattr(db, "close", None)
        if callable(close):
            close()


def probe_main(argv: list[str] | None = None) -> int:
    """scripts/ab/implementer_tier_probe.py — thin shell over this."""
    import argparse
    import os
    from pathlib import Path

    ap = argparse.ArgumentParser(
        prog="implementer_tier_probe",
        description="Two fresh implementer arms (shipped flash vs family upgrade) "
                    "over the SAME frozen corpus documents; promotion-noul verdict.")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan; write nothing, call nothing")
    ap.add_argument("--dossiers", default="data/dossiers")
    ap.add_argument("--db", default="data/signals.db")
    ap.add_argument("--raw-dir", default="data/raw")
    ap.add_argument("--out-dir", default="data/probe")
    ap.add_argument("--per-domain", type=int, default=12)
    ap.add_argument("--vendor", type=int, default=2)
    ap.add_argument("--dtc", type=int, default=6)
    ap.add_argument("--model-b", default=MODEL_B)
    ap.add_argument("--model-c", default=MODEL_C,
                    help="a third (frontier) arm; empty string skips it")
    ap.add_argument("--budget-usd", type=float, default=6.0)
    ap.add_argument("--force", action="store_true",
                    help="run even when the estimate exceeds --budget-usd")
    args = ap.parse_args(argv)

    print("implementer tier probe — DRY-RUN: nothing written, nothing called"
          if args.dry_run else "implementer tier probe — LIVE")
    # 1) frozen corpus
    rows = extract_rows(Path(args.db), Path(args.dossiers), raw_dir=args.raw_dir)
    domains_all = sorted({str(r.get("domain")) for r in rows if r.get("domain")})
    # stratify over domains that ACTUALLY have eligible docs — a selected
    # domain with zero joined docs is a wasted budget slot (2026-11-06:
    # 3 of 6 chosen DTC domains had none)
    eligible = [d for d, n in Counter(
        d["domain"] for d in collect_docs(rows, domains_all,
                                          per_domain=args.per_domain)).items()
        if n >= 5]
    domains = stratify_domains(eligible, n_vendor=args.vendor, n_dtc=args.dtc)
    docs = collect_docs(rows, domains, per_domain=args.per_domain)
    per_dom = Counter(d["domain"] for d in docs)
    est = estimate_cost(len(domains), arms=2, cost_per_domain_usd=COST_PER_DOMAIN_USD)
    print(f"corpus: {len(rows)} rows | domains selected ({len(domains)}): {', '.join(domains)}")
    print(f"docs: {len(docs)} ({dict(per_dom)}) | per-domain cap {args.per_domain}")
    print(f"arms: A={MODEL_A} (shipped) | B={args.model_b} (family upgrade)"
          + (f" | C={args.model_c} (frontier)" if args.model_c else ""))
    print(f"cost estimate: ${est:.2f} implementer (observed ${COST_PER_DOMAIN_USD:.2f}/domain/arm)"
          f" | budget cap ${args.budget_usd:.2f}")
    if args.dry_run:
        print("dry-run complete — no keys touched, no calls made")
        return 0
    if est > args.budget_usd and not args.force:
        print(f"ABORT: estimate ${est:.2f} exceeds budget ${args.budget_usd:.2f} "
              "(shrink scope or pass --force)", file=sys.stderr)
        return 2

    # 2) keys + decider (the _lab_decider pattern: live or refuse)
    api_key = os.environ.get("OPENROUTER_API_KEY")
    from src.lab.__main__ import _lab_decider, _load_decide_cfg

    decide_cfg = _load_decide_cfg()
    from src.lab.__main__ import _lab_decider

    decider = _lab_decider(decide_cfg)
    consent = os.environ.get("SIGNALS_DECIDE_DATA_EXIT")
    if not api_key or decider is None or not consent:
        print("ABORT: live run needs OPENROUTER_API_KEY + TYPESAFE_API_KEY + "
              "SIGNALS_DECIDE_DATA_EXIT consent (the lab decider factory "
              "refused without them)", file=sys.stderr)
        return 2

    # 3) verify arm B's model id exists BEFORE spending (1-token call)
    for mid in (args.model_b, args.model_c):
        if mid:
            err = verify_model_id(mid, api_key)
            if err:
                print(f"ABORT: arm model id failed verification ({mid}): {err}",
                      file=sys.stderr)
                return 2

    # 4) both arms over the same docs
    gates_cfg = (decide_cfg.get("decider") or {}).get("gates") or {}
    out_dir = Path(args.out_dir) / f"tierprobe-{_ts()}"
    arms = [("arm-a", MODEL_A), ("arm-b", args.model_b)]
    if args.model_c:
        arms.append(("arm-c", args.model_c))
    summaries = {}
    for arm, model in arms:
        print(f"--- {arm}: {model} over {len(docs)} docs ---")
        rows_out = run_arm(arm, model, docs, decider, gates_cfg=gates_cfg,
                           out_path=out_dir / f"{arm}-claims.jsonl",
                           api_key=api_key)
        summaries[arm] = summarize_nouls([r["promotion_noul"] for r in rows_out
                                          if r.get("promotion_noul") is not None])
        print(f"{arm}: {len(rows_out)} accepted claims | {summaries[arm]}")

    # 5) verdict + summary
    # the verdict compares arm A against the STRONGEST challenger (best p90)
    challengers = {k: v for k, v in summaries.items() if k != "arm-a"}
    best = max(challengers, key=lambda k: challengers[k].get("p90") or 0)
    comparison = compare_arms(summaries["arm-a"], summaries[best])
    comparison["best_arm"] = best
    summary = _render_summary(docs, domains, summaries, comparison)
    summary_path = out_dir / "summary.md"
    summary_path.write_text(summary, encoding="utf-8", newline="\n")
    print(f"VERDICT: {comparison['verdict']}")
    print(f"summary: {summary_path}")
    return 0


def _ts() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def verify_model_id(model: str, api_key: str) -> str | None:
    """One 1-token completion; returns the error text or None (live-only —
    a few thousandths of a cent)."""
    import json as _json
    import urllib.request

    body = _json.dumps({
        "model": model, "max_tokens": 4,
        "messages": [{"role": "user", "content": "Say OK"}],
    }).encode()
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions", data=body,
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = _json.loads(resp.read())
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    if data.get("error"):
        return str(data["error"])
    return None


def _render_summary(docs: list[dict], domains: list[str],
                    summaries: dict, comparison: dict) -> str:
    lines = [
        "# Implementer tier probe — summary", "",
        f"docs: {len(docs)} across {len(domains)} domains ({', '.join(domains)})",
        f"arm A (shipped): {MODEL_A}", f"arm B (upgrade): {MODEL_B}", "",
        "| arm | n | max | p50 | p90 | >=0.70 | [0.45,0.70) | >=0.45 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for arm in sorted(summaries):
        s = summaries[arm]
        lines.append(
            f"| {arm} | {s['n']} | {s['max']} | {s['p50']} | {s['p90']} "
            f"| {s['ge_floor']} | {s['band_released']} | {s['ge_045']} |")
    lines += ["", f"**VERDICT: {comparison['verdict']}**",
              f"(p90 moved {comparison['moved_p90']}, max moved {comparison['moved_max']})", "",
              "Decision rule (pre-registered): MODEL-TIER if ge_floor >= 1 or p90 moved >= 0.15; "
              "PROSE-BOUND if max moved <= 0.10 with zero promotions; else INCONCLUSIVE.",
              "Both arms fresh (fresh-vs-fresh); recorded claims reference-only.", ""]
    return "\n".join(lines)
