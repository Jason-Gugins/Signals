"""src/lab/tierprobe.py — the implementer tier probe's pure math + offline
wiring. The live question: does the promotion-noul ceiling (recorded max
0.34 vs floor 0.70) move with a stronger implementer on the SAME frozen
documents (MODEL-TIER ceiling), or is the input prose the binding
constraint (PROSE-BOUND)? Both arms fresh; recorded claims reference-only
(the 2026-11-06 fresh-vs-recorded instability)."""

import json

from src.lab.tierprobe import estimate_cost, stratify_domains, summarize_nouls


def test_stratify_domains_splits_vendor_and_dtc():
    domains = [
        "rillet.com", "databricks.com", "abnormal.ai",          # vendor
        "brooklinen.com", "brumate.com", "bubbleskincare.com",  # DTC
        "sakara.com", "ridge.com",
    ]
    picked = stratify_domains(domains, n_vendor=2, n_dtc=2)
    assert picked == sorted(picked) and len(picked) == 4
    # sorted-prefix semantics: the FIRST two vendors alphabetically
    assert "databricks.com" in picked and "abnormal.ai" in picked
    assert "brooklinen.com" in picked and "brumate.com" in picked


def test_stratify_domains_dtc_weighted_default():
    # the probe's default: 6 DTC + 2 vendor — the vendor arm is the
    # by-construction control, the spend concentrates on the ICP arm
    domains = ["databricks.com", "brooklinen.com", "brumate.com",
               "bubbleskincare.com", "sakara.com", "ridge.com",
               "lovevery.com", "magicspoon.com"]
    picked = stratify_domains(domains)
    dtc = [d for d in picked if d != "databricks.com"]
    assert len(picked) == 7                    # 1 available vendor + 6 DTC
    assert len(dtc) == 6


def test_summarize_nouls_reports_the_distribution():
    nouls = [0.10, 0.20, 0.34, 0.45, 0.52, 0.70, 0.81]
    s = summarize_nouls(nouls)
    assert s["n"] == 7 and s["max"] == 0.81
    assert s["ge_floor"] == 2          # >= 0.70
    assert s["band_released"] == 2     # [0.45, 0.70)
    assert s["ge_045"] == 4
    assert 0.3 < s["p50"] <= 0.45 and 0.5 < s["p90"] <= 0.81


def test_summarize_nouls_empty_is_honest():
    s = summarize_nouls([])
    assert s["n"] == 0 and s["max"] is None and s["p50"] is None


def test_estimate_cost_bounds_the_spend():
    # ~33c/domain observed at arm A (flash); scales linearly in arms
    est = estimate_cost(domains=8, arms=2, cost_per_domain_usd=0.33)
    assert est == 5.28


def test_run_arm_wiring_offline(tmp_path, monkeypatch):
    """run_arm() drives the REAL seams with fakes: implementer returns 2
    claims (verbatim quotes present, so the fabricated-quote prefilter
    passes); the citation decider accepts both; the promotion decider
    returns a real noul. Every accepted claim lands in the output rows with
    its noul, and the implementer model rides through unchanged."""
    import src.llm.implement as llm_implement
    import src.lab.tierprobe as tp
    from src.decide.jev import Decision

    seen_models = []

    def fake_call(messages, *, model, base_url, api_key, timeout_s):
        seen_models.append(model)
        # call_implementer's contract: the PARSED payload (it extracts the
        # chat content itself) — parse_claims reads raw["claims"] directly
        return {"claims": [
            {"text": "Acme automates invoicing for Shopify brands.",
             "quote": "automates invoicing for Shopify brands"},
            {"text": "Acme sponsors a local football team.",
             "quote": "sponsors a local football team"},
        ]}

    class FakeDecider:
        model = "jev-fake"

        def __init__(self):
            self.gates = []

        def decide(self, state, questions, gate=None):
            self.gates.append(gate)
            answers = {}
            for qid, spec in (questions or {}).items():
                if (spec or {}).get("type") == "choice":
                    answers[qid] = {"choice": "supports", "confidence": 1,
                                    "probabilities": {"supports": 0.95,
                                                      "says_nothing": 0.03,
                                                      "contradicted": 0.02},
                                    "type": "choice"}
                else:
                    answers[qid] = {"type": "noul", "noul": 0.10}
            if "promotion" in (questions or {}):
                answers["promotion"] = {"type": "noul", "noul": 0.42}
            return Decision(applies=True, ok=True, answers=answers, raw_tokens=5)

    monkeypatch.setattr(llm_implement, "call_implementer", fake_call)
    decider = FakeDecider()
    docs = [{"doc_id": "d1",
             "text": "Acme automates invoicing for Shopify brands. "
                     "Acme sponsors a local football team.",
             "domain": "acme.com"}]
    rows = tp.run_arm(
        "arm-a", "deepseek/deepseek-v4.1-flash", docs, decider,
        gates_cfg={"citation_soundness": {"enabled": True, "floor": 0.70},
                   "output_screen": {"enabled": True, "review_threshold": 0.45,
                                     "action_threshold": 0.70},
                   "need_promotion": {"enabled": True, "floor": 0.70,
                                      "band_low": 0.30}},
        out_path=tmp_path / "arm-a.jsonl",
    )
    assert seen_models == ["deepseek/deepseek-v4.1-flash"]
    assert len(rows) == 2                      # both claims citation-accepted
    assert all(r["arm"] == "arm-a" for r in rows)
    assert all(r["promotion_noul"] == 0.42 for r in rows)
    assert rows[0]["domain"] == "acme.com" and rows[0]["doc_id"] == "d1"
    assert {r["claim"] for r in rows} == {
        "Acme automates invoicing for Shopify brands.",
        "Acme sponsors a local football team.",
    }
    assert (tmp_path / "arm-a.jsonl").exists()


def test_collect_docs_caps_and_filters(tmp_path):
    from src.lab.tierprobe import collect_docs

    def doc_row(domain, doc_id, outcome="included", joined=True):
        return {"gate": "document_gate", "domain": domain, "doc_id": doc_id,
                "doc_joined": joined, "outcome": outcome, "text": f"text {doc_id}"}

    rows = [doc_row("acme.com", f"d{i}") for i in range(5)]
    rows += [doc_row("beta.io", "b1")]
    rows += [doc_row("acme.com", "dx", outcome="excluded")]
    rows += [doc_row("acme.com", "dn", joined=False)]
    picked = collect_docs(rows, ["acme.com"], per_domain=3)
    assert [p["doc_id"] for p in picked] == ["d0", "d1", "d2"]  # capped, ordered
    assert all(p["domain"] == "acme.com" for p in picked)
    all_acme = collect_docs(rows, ["acme.com", "beta.io"], per_domain=99)
    assert len(all_acme) == 5 + 1               # excluded + unjoined dropped
