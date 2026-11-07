"""Careers harvest Phase 2 — gate careers docs through the decide pipeline
and measure vs the homepage baseline (tier-probe arm A, fresh flash, 662
claims: max 0.76, p50 0.17, p90 0.36, ge_floor 2).

Pre-registered verdict rule (verify the baseline premise before letting it
fire — the tier-probe lesson):
- HARVEST WINS: careers-claim need-density >= 3x homepage's OR p90 >= 0.50
  with >= 1 non-price-promotion floor-crosser.
- FLAT: neither.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.lab.tierprobe import (  # noqa: E402
    MODEL_A, collect_docs, extract_rows, run_arm, summarize_nouls,
)

NEED = re.compile(
    r"\b(we need|needs? (?:help|a|an|better)|looking (?:for|to) (?:hire|find|improve)"
    r"|is hiring|are hiring|hiring (?:for|a)|struggl\w+|outgrown|switch(?:ing|ed)? from"
    r"|considering|evaluating|manual process|too slow|can'?t keep up|pain point"
    r"|frustrat\w+|wish (?:we|they)|tired of|hard to (?:keep|manage|track)"
    r"|scaling (?:our|beyond)|inefficient)\b", re.I)

PRICE_PROMO = re.compile(r"stocking stuffer|markets? \w+ under \$|sale|discount|\boff\b.*%|promo", re.I)


def need_density(claims: list[str]) -> dict:
    hits = [c for c in claims if NEED.search(c)]
    return {"n": len(claims), "need_hits": len(hits),
            "density": round(len(hits) / len(claims), 4) if claims else None}


def _load_docs(db, cap: int) -> list[dict]:
    """Careers docs: source='careers' first (this harvest), then the
    pre-existing career-URL docs on DTC corpus domains, capped. The domain
    set derives from the CORPUS (dossier packages) — the previous
    denylist let non-corpus vendor domains leak (lightspeedhq,
    boostsecurity, citylitics) and inflated the aggregate density."""
    import re
    from src.lab.tierprobe import VENDOR_DOMAINS

    pkg_re = re.compile(r"^(?P<domain>.+?)-[0-9]{8}T[0-9]{6}")
    corpus_domains = {m.group("domain")
                      for m in (pkg_re.match(pp.name)
                                for pp in Path("data/dossiers").iterdir())
                      if m}
    dtc = sorted(d for d in corpus_domains if d not in VENDOR_DOMAINS)
    picked: dict[str, dict] = {}
    for sql, params in (
        ("SELECT doc_id, domain, url, path FROM documents WHERE source='careers' "
         "ORDER BY fetched_at", ()),
        ("SELECT doc_id, domain, url, path FROM documents WHERE (url LIKE '%career%' OR url LIKE '%jobs%') "
         "AND domain IN (" + ",".join("?" * len(dtc)) + ") ORDER BY fetched_at", tuple(dtc)),
    ):
        for doc_id, domain, url, path in db.conn.execute(sql, params).fetchall():
            if doc_id in picked or len(picked) >= cap:
                continue
            p = path or f"data/raw/{doc_id[:2]}/{doc_id}"
            try:
                body = gzip.open(p, "rb").read().decode("utf-8", "replace")
            except Exception:
                continue
            if len(body.strip()) < 200:
                continue
            picked[doc_id] = {"doc_id": doc_id, "domain": domain, "url": url,
                              "text": body[:12000]}
    return list(picked.values())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--db", default="data/signals.db")
    ap.add_argument("--dossiers", default="data/dossiers")
    ap.add_argument("--raw-dir", default="data/raw")
    ap.add_argument("--out-dir", default="data/probe")
    ap.add_argument("--cap", type=int, default=24)
    args = ap.parse_args(argv)

    from src.core.db import Database
    from src.lab.__main__ import _lab_decider, _load_decide_cfg

    db = Database(args.db)
    docs = _load_docs(db, args.cap)
    by_dom = Counter(d["domain"] for d in docs)
    print(f"careers docs selected: {len(docs)} ({dict(by_dom)})")

    decide_cfg = _load_decide_cfg()
    decider = _lab_decider(decide_cfg)
    if decider is None:
        print("ABORT: no live decider (TYPESAFE_API_KEY / consent)", file=sys.stderr)
        return 2
    import os

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("ABORT: no OPENROUTER_API_KEY", file=sys.stderr)
        return 2

    gates_cfg = (decide_cfg.get("decider") or {}).get("gates") or {}
    out_dir = Path(args.out_dir) / f"careers-harvest-{_ts()}"
    rows = run_arm("careers", MODEL_A, docs, decider, gates_cfg=gates_cfg,
                   out_path=out_dir / "careers-claims.jsonl", api_key=api_key)
    nouls = [r["promotion_noul"] for r in rows if r.get("promotion_noul") is not None]
    care_sum = summarize_nouls(nouls)
    care_dens = need_density([r["claim"] for r in rows])

    # the homepage baseline: the tier probe's arm A (fresh flash, SAME seam)
    home_file = sorted(Path(args.out_dir).glob("tierprobe-*/arm-a-claims.jsonl"))[-1]
    home_rows = [json.loads(l) for l in open(home_file, encoding="utf-8")]
    home_nouls = [r["promotion_noul"] for r in home_rows if r.get("promotion_noul") is not None]
    home_sum = summarize_nouls(home_nouls)
    home_dens = need_density([r["claim"] for r in home_rows])

    print("\n=== careers vs homepage (both fresh flash, same seam) ===")
    for name, s, d in (("careers", care_sum, care_dens), ("homepage", home_sum, home_dens)):
        print(f"{name:9} n={s['n']:4} max={s['max']} p50={s['p50']} p90={s['p90']} "
              f"ge_floor={s['ge_floor']} ge_045={s['ge_045']} | "
              f"need-density={d['density']} ({d['need_hits']}/{d['n']})")

    # pre-registered verdict — with premise verification
    floor_crossers = [r for r in rows if (r.get("promotion_noul") or 0) >= 0.70
                      and not PRICE_PROMO.search(r["claim"])]
    density_ratio = ((care_dens["density"] or 0) / (home_dens["density"] or 1))
    if (care_dens["density"] or 0) >= 3 * (home_dens["density"] or 0) and care_dens["need_hits"] >= 5:
        verdict = "HARVEST WINS — careers prose carries materially more need language"
    elif (care_sum["p90"] or 0) >= 0.50 and len(floor_crossers) >= 1:
        verdict = "HARVEST WINS — the noul distribution's head moved with genuine floor-crossers"
    elif (care_sum["p90"] or 0) >= (home_sum["p90"] or 0) + 0.10 or (care_dens["density"] or 0) > (home_dens["density"] or 0) * 1.5:
        verdict = "PARTIAL — careers prose moves the needle but under the win bar; scale the harvest"
    else:
        verdict = "FLAT — careers prose (as collected) does not move the wall either; record why"
    print(f"\nVERDICT: {verdict}")
    print(f"floor-crossers (non-price-promo): {len(floor_crossers)}")
    for r in floor_crossers[:6]:
        print(f"  [{r['promotion_noul']:.2f}] {r['domain']:22} {r['claim'][:100]}")

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(json.dumps({
        "careers": {"summary": care_sum, "density": care_dens},
        "homepage": {"summary": home_sum, "density": home_dens},
        "verdict": verdict, "density_ratio": round(density_ratio, 2),
        "floor_crossers_non_promo": len(floor_crossers),
    }, indent=1, default=str), encoding="utf-8")
    print(f"wrote {out_dir / 'summary.json'}")
    return 0


def _ts() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


if __name__ == "__main__":
    sys.exit(main())
