"""Promotion-question A/B (the G5 intent-bar redesign, decide-lab addendum
11). The shipped question mis-reads the SUBJECT's selling as buyer intent
(stocking-stuffer artifact: noul 0.72-0.76, x7) and misses role-based
operational burden (careers duty claims: 0.2-0.59). The operator's 24
calibration labels referee two question variants; the verdict rule is
pre-registered below and every leg is paired within the quote-span regime
(the mandatory V0 quote-rebaseline — the recorded nouls used doc-anchored
excerpts, a different evidence regime). Ties count as losses.

Generalization limits (record verbatim in the summary): the verdict
generalizes ONLY to G5 ranking quality on flash-extracted DTC
homepage/careers claims in these strata, under quote-span evidence, on the
TYPESAFE decider (jev-1.13.0). It does NOT generalize to corpus-wide
promote rate/precision/recall, doc-anchored production scoring, non-DTC
verticals, or profile-relative need-ness. Disclosure: labels are
single-rater, revised post-hoc AFTER the scores were seen (the rationale
is construct-level — profile-blindness — independent of the scores, but
the timing is disclosed).
"""

from __future__ import annotations

import json
import random
from pathlib import Path

_FLOOR = 0.70
_U_WIN = 22          # exact permutation p = 0.019 on C(10,4)=210
_U_PARTIAL = 20      # nominal p = 0.057 — one pre-committed iteration

SHIPPED_TEXT = (
    "Does this extracted need represent genuine buying intent? Answer noul.")

VARIANTS: dict[str, dict] = {
    "V1": {"type": "noul", "instructions": (
        "Perspective: the company in the excerpt is the SUBJECT. Judge "
        "whether the excerpt shows the SUBJECT carrying an OPERATIONAL "
        "BURDEN that outside software or services could relieve — duties "
        "someone must run, tooling being evaluated or outgrown, processes "
        "straining at scale, staffing a function that needs support. The "
        "SUBJECT selling its own products (prices, promotions, launches, "
        "marketing) is NOT a need. Answer noul.")},
    "V2": {"type": "noul", "instructions": (
        "Perspective: the company in the excerpt is the SUBJECT. Judge "
        "whether the excerpt shows the SUBJECT carrying an OPERATIONAL "
        "BURDEN that outside software or services could relieve — duties "
        "someone must run, tooling being evaluated or outgrown, processes "
        "straining at scale, staffing a function that needs support. The "
        "SUBJECT selling its own products (prices, promotions, launches, "
        "marketing) is NOT a need. A STATED duty that maps to running or "
        "evaluating operational tooling counts as a burden signal; the "
        "mere existence of a hiring requisition does not. Answer noul.")},
}

# idx-keyed referee map (strata are NOT class-pure — home_high_noul splits
# NOT/NEED; the operator's revision moved hiring announcements to CONTEXT)
REFEREE_MAP = {
    4: "NEED", 22: "NEED", 23: "NEED", 24: "NEED",
    1: "NOT", 2: "NOT", 3: "NOT", 5: "NOT", 6: "NOT", 7: "NOT",
    9: "CONTEXT", 10: "CONTEXT", 14: "CONTEXT", 15: "CONTEXT",
    16: "CONTEXT", 17: "CONTEXT", 18: "CONTEXT", 19: "CONTEXT",
    20: "CONTEXT", 21: "CONTEXT",
    11: "GUARD", 12: "GUARD", 13: "GUARD",
    8: "ORPHAN",
}

# exact permutation p-table (C(10,4)=210 label assignments over the clean set)
PERM_P = {20: 0.057, 21: 0.033, 22: 0.019, 23: 0.0095, 24: 0.0048}


def load_labeled_claims(worksheet_path, sample_path) -> list[dict]:
    """Parse the operator's [X] dispositions (idx-keyed), join the sample's
    hidden nouls/quote_spans, attach the referee class per REFEREE_MAP."""
    import re

    text = Path(worksheet_path).read_text(encoding="utf-8")
    labels: dict[int, str] = {}
    for block in text.split("## ")[1:]:
        m = re.match(r"(\d+)\.", block)
        if not m:
            continue
        idx = int(m.group(1))
        if "[X] NEED" in block:
            labels[idx] = "NEED"
        elif "[X] NOT-A-NEED" in block:
            labels[idx] = "NOT"
        elif "[X] UNSURE" in block:
            labels[idx] = "UNSURE"
    out = []
    for o in json.loads(Path(sample_path).read_text(encoding="utf-8")):
        idx = o["idx"]
        out.append({**o, "operator_label": labels.get(idx, "?"),
                    "referee": REFEREE_MAP.get(idx, "ORPHAN")})
    return out


def _pctl(sorted_vals, q):
    if not sorted_vals:
        return None
    return sorted_vals[min(len(sorted_vals) - 1, max(0, round(q * (len(sorted_vals) - 1))))]


def summarize_nouls(nouls):
    vs = sorted(v for v in nouls if isinstance(v, (int, float)))
    return {"n": len(vs), "max": vs[-1] if vs else None,
            "p50": _pctl(vs, 0.5), "p90": _pctl(vs, 0.9),
            "ge_floor": sum(1 for v in vs if v >= _FLOOR)}


def referee_verdict(*, U: int, promo_max: float, ctx_max: float,
                    duty_mean: float, duty_mean_v0_rebaseline: float,
                    guard_max: float) -> dict:
    """Pre-registered rule v2. All legs paired within the quote-span regime
    (the V0 quote-rebaseline is mandatory). Ties count as losses."""
    suppressed = promo_max < _FLOOR and promo_max < duty_mean_v0_rebaseline
    guard_ok = guard_max < _FLOOR
    lifted = duty_mean > duty_mean_v0_rebaseline
    if U >= _U_WIN and suppressed and guard_ok and lifted:
        verdict = "WIN — artifacts suppressed, duty claims lifted, requisitions not inflated"
    elif U >= _U_WIN:
        verdict = "PARTIAL — separation cleared (U >= 22) with a leg failed; iterate the wording ONCE"
    elif U >= _U_PARTIAL:
        verdict = "PARTIAL — separation improved into the iterate band (U in [20,21]); ONE pre-committed iteration"
    else:
        verdict = ("NO-CHANGE — U <= 19: the failure lives in the decider model, "
                   "not the wording; record and stop")
    return {"verdict": verdict, "U": U, "promo_max": promo_max,
            "ctx_max": ctx_max, "guard_max": guard_max,
            "duty_lift": round(duty_mean - duty_mean_v0_rebaseline, 3)}


def rescore(claims: list[dict], pass_name: str, decider, *,
            gates_cfg: dict, out_path: Path, passes: int = 1) -> list[dict]:
    """One scoring pass: re-ask the promotion question over pre-extracted
    claims with `question_override` riding gate_need_promotion. pass_name
    "V0" = the SHIPPED question (the quote-rebaseline); "V1"/"V2" = the
    variants. `passes` > 1 averages the decider's nondeterminism (k-pass).
    Probe-local DecideLedger — zero DB side effects. The implementer seam
    is NOT touched (claims are pre-extracted)."""
    from src.decide.gates import DecideLedger, gate_need_promotion

    promotion_cfg = gates_cfg.get("need_promotion") or {}
    override = ({"type": "noul", "instructions": SHIPPED_TEXT}
                if pass_name == "V0" else VARIANTS[pass_name])
    ledger = DecideLedger()
    run_id = f"promo-ab-{pass_name.lower()}"
    key = f"{pass_name.lower()}_noul"
    rows: list[dict] = []
    for c in claims:
        nouls = []
        for _ in range(max(1, passes)):
            _promote, noul, _d = gate_need_promotion(
                c["claim"], c["quote_span"], False, decider, promotion_cfg,
                ledger, run_id, "enforce", quote_span=c["quote_span"],
                question_override=override,
            )
            nouls.append(noul if isinstance(noul, (int, float)) else 0.0)
        rows.append({**c, key: round(sum(nouls) / len(nouls), 4)})
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, default=str) + "\n")
    return rows


def probe_main(argv: list[str] | None = None) -> int:
    import argparse
    import os
    import sys

    ap = argparse.ArgumentParser(
        prog="promo_ab",
        description="Label-refereed promotion-question A/B over the 24 "
                    "calibration claims (V0 quote-rebaseline + V1 + V2).")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--worksheet", default="data/probe/PROMO_CALIBRATION_20261107.md")
    ap.add_argument("--sample", default="data/probe/PROMO_CALIBRATION_SAMPLE_20261107.json")
    ap.add_argument("--out-dir", default="data/probe")
    ap.add_argument("--passes", type=int, default=3,
                    help="k-pass averaging per claim per variant (the decider "
                         "is nondeterministic: the same claim scored 0.72-0.76 "
                         "across tier-probe arms)")
    ap.add_argument("--budget-calls", type=int, default=240)
    args = ap.parse_args(argv)

    claims = load_labeled_claims(args.worksheet, args.sample)
    partition = Counter(c["referee"] for c in claims)
    estimate = len(claims) * 3 * args.passes   # V0 + V1 + V2
    print("promotion-question A/B — DRY-RUN: nothing written, no calls"
          if args.dry_run else "promotion-question A/B — LIVE")
    print(f"claims: {len(claims)} | referee partition: "
          f"{partition['NEED']} NEED, {partition['NOT']} NOT, "
          f"{partition['CONTEXT']} CONTEXT, {partition['GUARD']} GUARD, "
          f"{partition['ORPHAN']} ORPHAN")
    print(f"passes: {args.passes} per claim per variant (nondeterminism "
          f"averaging) | calls: {estimate} | budget cap {args.budget_calls}")
    print(f"variants: V0 (shipped, quote-rebaseline), V1, V2")
    if estimate > args.budget_calls:
        print(f"ABORT: estimate {estimate} calls exceeds budget "
              f"{args.budget_calls}", file=sys.stderr)
        return 2
    if args.dry_run:
        print("dry-run complete — no keys touched, no calls made")
        return 0

    if partition["NEED"] != 4 or partition["NOT"] != 6:
        print("ABORT: the referee partition changed — the pre-registered "
              "bars (U out of 24 pairs) assume 4 duty-NEEDs x 6 NOTs",
              file=sys.stderr)
        return 2

    from src.core.config import Config  # noqa: F401  (dotenv side effect)
    from src.lab.__main__ import _lab_decider, _load_decide_cfg

    decide_cfg = _load_decide_cfg()
    decider = _lab_decider(decide_cfg)
    if decider is None:
        print("ABORT: no live decider (TYPESAFE_API_KEY / consent)", file=sys.stderr)
        return 2

    gates_cfg = (decide_cfg.get("decider") or {}).get("gates") or {}
    out_dir = Path(args.out_dir) / f"promo-ab-{_ts()}"
    print(f"--- V0 quote-rebaseline (shipped question) ---")
    v0_rows = rescore(claims, "V0", decider, gates_cfg=gates_cfg,
                      out_path=out_dir / "v0-rows.jsonl", passes=args.passes)
    results: dict[str, list[dict]] = {"V0": v0_rows}
    for vid in ("V1", "V2"):
        print(f"--- {vid} ---")
        results[vid] = rescore(claims, vid, decider, gates_cfg=gates_cfg,
                               out_path=out_dir / f"{vid.lower()}-rows.jsonl",
                               passes=args.passes)

    v0_by_idx = {r["idx"]: r for r in v0_rows}
    duty_mean_v0 = _mean([v0_by_idx[i]["v0_noul"] for i in (4, 22, 23, 24)])
    lines = ["# Promotion-question A/B — summary", "",
             "Generalization limits: G5 ranking quality on flash-extracted DTC",
             "homepage/careers claims in these strata, under quote-span evidence,",
             "on the TYPESAFE decider. NOT corpus-wide promote rates, NOT",
             "doc-anchored production scoring, NOT profile-relative need-ness.",
             "Disclosure: labels are single-rater, revised post-hoc after the",
             "scores were seen (rationale: construct-level profile-blindness).", "",
             f"passes: {args.passes} per claim per variant (nondeterminism averaging)", "",
             "| idx | referee | operator | V0 | V1 | V2 |", "|---|---|---|---|---|---|"]
    for c in claims:
        i = c["idx"]
        vals = [results[p][i][f"{p.lower()}_noul"] for p in ("V0", "V1", "V2")]
        lines.append(f"| {i} | {c['referee']} | {c['operator_label']} | "
                     f"{vals[0]:.2f} | {vals[1]:.2f} | {vals[2]:.2f} |")
    lines += ["", "| pass | duty_mean | promo_max | ctx_max | guard_max | U | verdict |",
              "|---|---|---|---|---|---|---|"]
    verdicts = {}
    for vid in ("V1", "V2"):
        by_idx = {r["idx"]: r for r in results[vid]}
        key = f"{vid.lower()}_noul"
        duty_mean = _mean([by_idx[i][key] for i in (4, 22, 23, 24)])
        promo_max = max(by_idx[i][key] for i in (1, 3))
        ctx_max = max(by_idx[i][key] for i in (9, 10, 14, 15, 16, 17, 18, 19, 20, 21))
        guard_max = max(by_idx[i][key] for i in (11, 12, 13))
        U = sum(1 for i in (4, 22, 23, 24)
                if by_idx[i][key] > v0_by_idx[i]["v0_noul"]
                and all(by_idx[i][key] > v0_by_idx[j]["v0_noul"]
                        for j in (1, 2, 3, 5, 6, 7)))
        v = referee_verdict(U=U, promo_max=promo_max, ctx_max=ctx_max,
                            duty_mean=duty_mean,
                            duty_mean_v0_rebaseline=duty_mean_v0,
                            guard_max=guard_max)
        verdicts[vid] = v
        lines.append(f"| {vid} | {duty_mean:.2f} | {promo_max:.2f} | {ctx_max:.2f} "
                     f"| {guard_max:.2f} | {U}/24 | {v['verdict']} |")
    lines += ["", "Permutation p-table (exact, C(10,4)=210): U=20: 0.057, "
              "U=21: 0.033, U=22: 0.019, U=24: 0.0048. Ties = losses.", ""]
    summary_path = out_dir / "summary.md"
    summary_path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    print(f"\nVERDICTS: V1 = {verdicts['V1']['verdict']}")
    print(f"          V2 = {verdicts['V2']['verdict']}")
    print(f"summary: {summary_path}")
    return 0


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _ts() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


from collections import Counter  # noqa: E402  (probe_main partition counts)

if __name__ == "__main__":
    import sys

    sys.exit(probe_main())
