#!/usr/bin/env python
"""A/B split-test report for the retrieval posture experiment.

Adapted from tmp/ab_final_report.py (2026-10 experiment; the original stays
in tmp/ as history). See scripts/ab/README.md for the runbook.

Reconstructs each arm's staged-doc set by recomputing the document gate's
state_hash per corpus document (state = {"doc_id", "source": account_name,
"text": truncate(text, 9600)}) and matching it against the gate rows in each
package's decisions.jsonl — the ledger does not persist doc_id directly.
This reconstruction is the proven method; since extraction-before-claims it
is a DUAL-hash join (src.lab.corpus.doc_gate_hashes, Task 2b): pre-extraction
packages hashed the raw body head, post-extraction packages the
doccontent-extracted head, so each document keys BOTH and a gate row joins
on either. The account_name term is read from the accounts table (the name
run_intel had in scope when each arm ran), so new domains work without a
hardcoded map.

Usage (run from anywhere; paths resolve from the repo root):
  python scripts/ab/report.py [--ledger tmp/ab_arms.tsv]
      [--out data/probe/RETRIEVAL_AB_2026_10.md] [domain ...]

With no arguments this reproduces the 2026-10 report (the 10-domain corpus,
the experiment ledger, the original output path). WARNING: the default
--out is the committed 2026-10 record — pass --out for re-tests.
"""
import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, ".")  # the original's cwd=repo-root contract
sys.path.insert(0, str(REPO_ROOT))  # takes precedence when run from elsewhere

from src.core.config import Config
from src.core.db import Database
from src.core.rawstore import RawStore
from src.lab.corpus import doc_gate_hashes

DEFAULT_LEDGER = "tmp/ab_arms.tsv"
DEFAULT_OUT = "data/probe/RETRIEVAL_AB_2026_10.md"
DEFAULT_DOMAINS = [
    "deepinfra.com", "amigo.ai", "cyera.com", "databricks.com",
    "decagon.ai", "elevenlabs.io", "ramp.com", "helcim.com",
    "veeda.com", "rillet.com",
]


def rows_of(pkg: Path) -> list[dict]:
    f = pkg / "decisions.jsonl"
    if not f.exists():
        return []
    return [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]


def dossier_of(pkg: Path) -> dict:
    f = pkg / "dossier.json"
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def account_name(db: Database, domain: str) -> str:
    """The name run_intel had in scope for this domain.

    run_intel keeps an existing account's DB name (and derives
    ``domain.split('.')[0].capitalize()`` for new accounts), so the accounts
    table is the authoritative ``source`` term for the state-hash match.
    """
    row = db.one("SELECT name FROM accounts WHERE domain = ?", (domain,))
    if row and row["name"]:
        return str(row["name"])
    return domain.split(".")[0].capitalize()


def staged_by_label(dossB: dict, n_staged_b: int) -> str:
    """How arm B staged its docs, read from the package itself.

    The dossier's ``gaps`` carry the retrieval fallback line verbatim —
    e.g. ``retrieval: index coverage 0.00 for d.com (staged_by: uncovered);
    ... staging fell back to recency`` — so parse the actual reason when
    present instead of guessing. With no gap line the run either staged via
    retrieval (the protocol for arm B) or the retrieval block never ran;
    those are indistinguishable in the package, so fall back to the
    original count bound (>~stage_docs 35 recency-scale sets ⇒ suspicious).
    """
    gaps = " | ".join(str(g) for g in (dossB.get("gaps") or []))
    if "retrieval staging failed" in gaps:
        return "error (recency fallback)"
    m = re.search(r"staged_by:\s*(\w+)", gaps)
    if m:
        return f"{m.group(1)} (recency fallback)"
    if n_staged_b > 40:
        return "retrieval off? (recency-scale staged set, no gap line)"
    return "retrieval"


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="report.py",
        description="Summarize a retrieval A/B split test from the arm ledger + dossier packages.",
        epilog="All paths resolve from the repo root regardless of cwd.",
    )
    p.add_argument("--ledger", default=DEFAULT_LEDGER, metavar="TSV",
                   help="arm ledger TSV: arm\\tdomain\\tpackage\\trc=N [default: %(default)s]")
    p.add_argument("--out", default=DEFAULT_OUT, metavar="MD",
                   help="output markdown path (the default is the committed 2026-10 record — "
                        "pass an explicit --out for re-tests) [default: %(default)s]")
    p.add_argument("domains", nargs="*", default=DEFAULT_DOMAINS, metavar="domain",
                   help="domains to report (need both arms ledgered) "
                        "[default: the 10-domain 2026-10 corpus]")
    return p.parse_args(argv)


def main() -> None:
    args = parse_args(sys.argv[1:])
    os.chdir(REPO_ROOT)  # the original's cwd contract; anchors every relative path

    ledger = Path(args.ledger)
    if not ledger.exists():
        sys.exit(f"report.py: ledger not found: {ledger} — run scripts/ab/run_split_test.sh first, or pass --ledger")

    cfg = Config.load()
    db = Database(cfg.storage.db_path)
    rs = RawStore(db, cfg.storage.raw_dir)

    arms: dict[str, dict[str, str]] = {"A": {}, "B": {}}
    for line in ledger.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[0] in arms:
            arms[parts[0]][parts[1]] = parts[2]

    domains = [d for d in args.domains if d in arms["A"] and d in arms["B"]]
    for d in args.domains:
        if d not in domains:
            missing = "A" if d not in arms["A"] else "B"
            print(f"report.py: skipping {d}: no arm {missing} row in {ledger}")
    if not domains:
        sys.exit(f"report.py: no domains with both arms ledgered in {ledger}")

    out = [
        f"# Retrieval A/B split test — results ({datetime.now(timezone.utc).date()})",
        "",
        "Control (A) = recency staging, retrieval off. Treatment (B) = hybrid",
        "retrieval staging (FTS5 BM25 + MiniLM vectors, RRF, ms-marco rerank,",
        "MMR), top-35 docs. Identical corpus (B ran `--skip collect`), same",
        "models, same gates, same market profile, enforce mode. Staged sets",
        "reconstructed by matching each arm's document-gate `state_hash` against",
        "per-document recomputed hashes (state = doc_id + account_name + text",
        "head-truncated to 9,600 chars; both the raw-body and the",
        "extraction-extracted head variants — dual-hash join).",
        "",
        f"Ledger: `{ledger}` (domains: {', '.join(domains)}).",
        "",
    ]
    tot = {"g4": [0, 0], "rev": [0, 0], "keep": [0, 0], "drop": [0, 0], "jev": [0, 0]}
    b_only_ages_all: list[float] = []
    for domain in domains:
        name = account_name(db, domain)
        pkgA = Path("data/dossiers") / arms["A"][domain]
        pkgB = Path("data/dossiers") / arms["B"][domain]
        rowsA, rowsB = rows_of(pkgA), rows_of(pkgB)

        doc_text, doc_age, doc_hashes = {}, {}, {}
        for meta in rs.iter_docs(domain=domain):
            did = str(getattr(meta, "doc_id", "") or "")
            try:
                full = rs.get(did)
            except Exception:
                continue
            body = getattr(full, "body", None) if full is not None else None
            text = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else str(body or "")
            doc_text[did] = text
            # Dual-hash join (Task 2b): both gate-state variants a doc can
            # carry — raw head (pre-extraction arms) and extracted head
            # (post-extraction arms); identical heads dedupe to one.
            doc_hashes[did] = {d for d, _ in doc_gate_hashes(
                did, name, text, getattr(full, "content_type", None)
            )}
            fa = str(getattr(meta, "fetched_at", "") or "")
            try:
                doc_age[did] = (datetime.now(timezone.utc) - datetime.fromisoformat(fa)).total_seconds() / 86400.0
            except Exception:
                pass

        def staged(rows):
            hashes = {r.get("state_hash") for r in rows if r.get("gate") == "document_gate"}
            return {
                did for did, variants in doc_hashes.items()
                if variants & hashes
            }

        sA, sB = staged(rowsA), staged(rowsB)
        gA = [r for r in rowsA if r.get("gate") == "citation_soundness"]
        gB = [r for r in rowsB if r.get("gate") == "citation_soundness"]

        def g4(rows):
            g = [r for r in rows if r.get("gate") == "citation_soundness"]
            o = Counter(r.get("outcome") for r in g)
            j = sum(r.get("raw_tokens") or 0 for r in rows)
            gates = Counter(r.get("gate") for r in rows)
            return o, j, gates

        oA, jA, gatesA = g4(rowsA)
        oB, jB, gatesB = g4(rowsB)
        dossB = dossier_of(pkgB)
        staged_by = staged_by_label(dossB, len(sB))

        b_only = sB - sA
        ages = sorted(doc_age.get(d) for d in b_only if doc_age.get(d) is not None)
        b_only_ages_all.extend(a for a in ages if a is not None)

        for i, v in enumerate((gA, gB)):
            tot["g4"][i] += len(v)
            oc = Counter(r.get("outcome") for r in v)
            tot["rev"][i] += oc.get("reviewed", 0)
            tot["keep"][i] += oc.get("kept", 0)
            tot["drop"][i] += oc.get("dropped", 0)
        tot["jev"][0] += jA
        tot["jev"][1] += jB

        out.append(f"## {name} ({domain})")
        out.append("")
        out.append("| metric | A (recency) | B (retrieval) |")
        out.append("|---|---|---|")
        out.append(f"| staged docs | {len(sA)} | {len(sB)} ({staged_by}) |")
        out.append(f"| gate excl. (of staged) | {len(sA) - sum(1 for r in rowsA if r.get('gate')=='document_gate' and r.get('deterministic_action')=='include')} | {len(sB) - sum(1 for r in rowsB if r.get('gate')=='document_gate' and r.get('deterministic_action')=='include')} |")
        out.append(f"| G4 verdicts | {len(gA)} | {len(gB)} |")
        out.append(f"| G4 reviewed/kept/dropped | {oA.get('reviewed',0)}/{oA.get('kept',0)}/{oA.get('dropped',0)} | {oB.get('reviewed',0)}/{oB.get('kept',0)}/{oB.get('dropped',0)} |")
        out.append(f"| G5 calls | {gatesA.get('need_promotion',0)} | {gatesB.get('need_promotion',0)} |")
        out.append(f"| Jev input tokens | {jA:,} | {jB:,} |")
        out.append(f"| Jev errored rows | {oA.get('errored',0)} | {oB.get('errored',0)} |")
        out.append("")
        out.append(f"- staged overlap A∩B: {len(sA & sB)}; B-only: {len(b_only)}; A-only: {len(sA - sB)}")
        if ages:
            out.append(f"- B-only doc ages (days): n={len(ages)} min={ages[0]:.1f} median={ages[len(ages)//2]:.1f} max={ages[-1]:.1f}")
        dA = dossier_of(pkgA)
        out.append(f"- decide_degraded: A={dA.get('decide_degraded')} B={dossB.get('decide_degraded')}")
        out.append("")

    if b_only_ages_all:
        s = sorted(b_only_ages_all)
        out.append(f"## B-only documents, all domains pooled")
        out.append("")
        out.append(f"n={len(s)}, ages (days): min={s[0]:.1f} p25={s[len(s)//4]:.1f} median={s[len(s)//2]:.1f} p75={s[3*len(s)//4]:.1f} max={s[-1]:.1f}")
        out.append("")

    out.append("## Headline totals")
    out.append("")
    out.append("| total | A | B |")
    out.append("|---|---|---|")
    out.append(f"| G4 verdicts | {tot['g4'][0]:,} | {tot['g4'][1]:,} |")
    out.append(f"| reviewed | {tot['rev'][0]:,} | {tot['rev'][1]:,} |")
    out.append(f"| kept | {tot['keep'][0]} | {tot['keep'][1]} |")
    out.append(f"| dropped | {tot['drop'][0]:,} | {tot['drop'][1]:,} |")
    out.append(f"| Jev input tokens | {tot['jev'][0]:,} | {tot['jev'][1]:,} |")
    out.append("")
    tok_delta = (tot["jev"][0] - tot["jev"][1]) / tot["jev"][0] * 100.0 if tot["jev"][0] else 0.0
    out.append("## Verdict")
    out.append("")
    out.append(
        f"- G4 kept: A={tot['keep'][0]} vs B={tot['keep'][1]}; reviewed A={tot['rev'][0]:,} vs"
        f" B={tot['rev'][1]:,}; dropped A={tot['drop'][0]:,} vs B={tot['drop'][1]:,}."
    )
    out.append(
        f"- Jev input tokens: A={tot['jev'][0]:,} vs B={tot['jev'][1]:,}"
        f" (B is {tok_delta:.0f}% below A) — the cost side of the thesis."
    )
    out.append(
        "- Compare G4-accepted and G5-promoted counts (the gates wave reports them"
        " separately) before reading `kept` as the whole outcome — the 2026-10 run"
        " showed a G5 wall behind G4. Baseline analysis for the original run:"
        " data/probe/RETRIEVAL_AB_2026_10.md."
    )

    dest = Path(args.out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(out) + "\n", encoding="utf-8")
    print("wrote", dest)
    print("\n".join(out[-16:]))


if __name__ == "__main__":
    main()
