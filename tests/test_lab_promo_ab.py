"""src/lab/promo_ab.py — the label-refereed promotion-question A/B.

The operator's 24 calibration labels referee two question variants through
the REAL gate_need_promotion seam (question_override). Pre-registered rule
v2 (two-agent verified): clean referee set = 4 duty-NEEDs x 6 NOTs = 24
pairs, U out of 24, ties = losses, exact-permutation bars (U>=22 => p=0.019),
mandatory V0 quote-rebaseline, 13-claim inflation guard, k-pass averaging
(the decider is nondeterministic: the same claim scored 0.72-0.76 across
tier-probe arms)."""

import json

from src.lab.promo_ab import (
    SHIPPED_TEXT, VARIANTS, load_labeled_claims, referee_verdict,
    summarize_nouls,
)


def test_variants_full_dicts_with_the_failure_mode_clauses():
    assert set(VARIANTS) == {"V1", "V2"}
    for v in VARIANTS.values():
        assert v["type"] == "noul"                     # the live call needs "type"
        assert "selling its own products" in v["instructions"]   # anti-commerce
        assert "OPERATIONAL BURDEN" in v["instructions"]         # perspective anchor
    # V2's NARROWED duty clause (operator revision): STATED duties count,
    # bare requisitions do not
    assert "duty" in VARIANTS["V2"]["instructions"].lower()
    assert "requisition" in VARIANTS["V2"]["instructions"].lower()
    assert "duty" not in VARIANTS["V1"]["instructions"].lower()


def test_shipped_text_is_verbatim_from_gates():
    assert SHIPPED_TEXT == (
        "Does this extracted need represent genuine buying intent? Answer noul.")


def test_load_labeled_claims_is_idx_keyed_and_maps_the_revision(tmp_path):
    ws = tmp_path / "ws.md"
    ws.write_text(
        "## 1. [promo_artifact] x\nDisposition: [ ] NEED   [X] NOT-A-NEED   [ ] UNSURE\n"
        "## 4. [home_high_noul] x\nDisposition: [X] NEED   [ ] NOT-A-NEED   [ ] UNSURE\n"
        "## 12. [home_need_language] x\nDisposition: [X] NEED   [ ] NOT-A-NEED   [ ] UNSURE\n"
        "## 14. [careers_role] x\nDisposition: [X] NEED   [ ] NOT-A-NEED   [ ] UNSURE\n",
        encoding="utf-8")
    sample = tmp_path / "s.json"
    sample.write_text(json.dumps([
        {"idx": 1, "stratum": "promo_artifact", "domain": "d", "claim": "c1",
         "quote_span": "q1", "url": "", "noul_hidden": 0.74, "source_file": "arm-a"},
        {"idx": 4, "stratum": "home_high_noul", "domain": "d", "claim": "c4",
         "quote_span": "q4", "url": "", "noul_hidden": 0.59, "source_file": "arm-a"},
        {"idx": 12, "stratum": "home_need_language", "domain": "d", "claim": "c12",
         "quote_span": "q12", "url": "", "noul_hidden": 0.23, "source_file": "careers"},
        {"idx": 14, "stratum": "careers_role", "domain": "d", "claim": "c14",
         "quote_span": "q14", "url": "", "noul_hidden": 0.26, "source_file": "careers"},
    ]), encoding="utf-8")
    out = load_labeled_claims(ws, sample)
    by_idx = {c["idx"]: c for c in out}
    # idx-keyed, NOT stratum-keyed: home_high_noul splits NOT(1)/NEED(4);
    # home_need_language is NEED-labeled but GUARDED; careers_role -> CONTEXT
    assert by_idx[1]["referee"] == "NOT"
    assert by_idx[4]["referee"] == "NEED"
    assert by_idx[12]["referee"] == "GUARD"
    assert by_idx[14]["referee"] == "CONTEXT"
    assert by_idx[4]["operator_label"] == "NEED"
    assert by_idx[4]["noul_hidden"] == 0.59


def test_referee_verdict_rule():
    # pre-registered rule v2: U out of 24 duty-vs-NOT pairs, ties = losses;
    # exact permutation bars (U>=22 => p=0.019; U>=20 => p=0.057)
    from src.lab.promo_ab import referee_verdict as rv
    # shipped-ranking state on the clean set: duty [0.59,0.53,0.48,0.46] vs
    # NOT [0.74,0.67,0.31,0.24,0.23,0.13] -> 16/24 = 0.667 -> NO-CHANGE
    v0 = {"U": 16, "promo_max": 0.74, "ctx_max": 0.31, "duty_mean": 0.515,
          "duty_mean_v0_rebaseline": 0.515, "guard_max": 0.31}
    win = {"U": 23, "promo_max": 0.44, "ctx_max": 0.40, "duty_mean": 0.72,
           "duty_mean_v0_rebaseline": 0.515, "guard_max": 0.40}
    suppress_only = {"U": 20, "promo_max": 0.44, "ctx_max": 0.40,
                     "duty_mean": 0.515, "duty_mean_v0_rebaseline": 0.515,
                     "guard_max": 0.40}   # artifacts die, duty unchanged
    partial = {"U": 20, "promo_max": 0.55, "ctx_max": 0.40, "duty_mean": 0.60,
               "duty_mean_v0_rebaseline": 0.515, "guard_max": 0.40}
    nochg = {"U": 14, "promo_max": 0.60, "ctx_max": 0.31, "duty_mean": 0.45,
             "duty_mean_v0_rebaseline": 0.515, "guard_max": 0.31}
    assert rv(**v0)["verdict"].startswith("NO-CHANGE")
    assert rv(**win)["verdict"].startswith("WIN")
    # the degenerate suppression-only case (U=20, duty unchanged) lands in
    # the PARTIAL iterate band, NOT a WIN — legs 1+4 compose to block it
    assert rv(**suppress_only)["verdict"].startswith("PARTIAL")
    assert rv(**suppress_only)["U"] < 22
    assert "PARTIAL" in rv(**partial)["verdict"]
    assert rv(**nochg)["verdict"].startswith("NO-CHANGE")


def test_rescore_uses_the_variant_question_and_unwraps_gate_cfg(tmp_path):
    import src.lab.promo_ab as pa
    from src.decide.jev import Decision

    seen = []

    class FakeDecider:
        model = "jev-fake"

        def decide(self, state, questions, gate=None):
            seen.append({"promotion": questions["promotion"],
                         "state_need": state["need"],
                         "state_excerpt": state["evidence_excerpt"]})
            noul = 0.80 if "stocking stuffer" in state["need"] else 0.45
            return Decision(applies=True, ok=True,
                            answers={"promotion": {"type": "noul", "noul": noul}},
                            raw_tokens=4)

    claims = [
        {"idx": 1, "referee": "NOT", "domain": "bubbleskincare.com",
         "claim": "Bubble markets stocking stuffers under $16.",
         "quote_span": "Stocking Stuffers Under $16", "noul_hidden": 0.74},
        {"idx": 4, "referee": "NEED", "domain": "brooklinen.com",
         "claim": "The role owns the full P&L and evaluates CX tooling fit.",
         "quote_span": "owns the full P&L", "noul_hidden": 0.59},
    ]
    rows = pa.rescore(claims, "V1", FakeDecider(),
                      gates_cfg={"citation_soundness": {"floor": 0.70},
                                "need_promotion": {"floor": 0.70, "band_low": 0.30}},
                      out_path=tmp_path / "v1.jsonl")
    assert len(rows) == 2
    assert all(s["promotion"]["type"] == "noul" for s in seen)      # full dict
    assert all("OPERATIONAL BURDEN" in s["promotion"]["instructions"] for s in seen)
    assert all(s["state_need"] == c["claim"] for s, c in zip(seen, claims))
    assert all("Stocking Stuffers Under $16" in s["state_excerpt"]
               or "owns the full P&L" in s["state_excerpt"] for s in seen)
    assert rows[0]["v1_noul"] == 0.80 and rows[0]["referee"] == "NOT"
    assert rows[1]["v1_noul"] == 0.45 and rows[1]["referee"] == "NEED"
    assert (tmp_path / "v1.jsonl").exists()


def test_rescore_v0_pass_uses_the_shipped_text(tmp_path):
    import src.lab.promo_ab as pa
    from src.decide.jev import Decision

    seen = []

    class FakeDecider:
        model = "jev-fake"

        def decide(self, state, questions, gate=None):
            seen.append(questions["promotion"]["instructions"])
            return Decision(applies=True, ok=True,
                            answers={"promotion": {"type": "noul", "noul": 0.5}},
                            raw_tokens=4)

    claims = [{"idx": 1, "referee": "NOT", "domain": "d",
               "claim": "c1", "quote_span": "q1", "noul_hidden": 0.5}]
    pa.rescore(claims, "V0", FakeDecider(), gates_cfg={"need_promotion": {}},
               out_path=tmp_path / "v0.jsonl")
    assert seen == [SHIPPED_TEXT]   # the rebaseline is the SHIPPED question


def test_cli_dry_run_prints_partition_and_spends_nothing(tmp_path, capsys):
    import src.lab.promo_ab as pa

    rc = pa.probe_main([
        "--dry-run", "--worksheet", "data/probe/PROMO_CALIBRATION_20261107.md",
        "--sample", "data/probe/PROMO_CALIBRATION_SAMPLE_20261107.json",
        "--out-dir", str(tmp_path / "out"),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert "4 NEED" in out and "6 NOT" in out and "10 CONTEXT" in out
    assert "3 GUARD" in out and "1 ORPHAN" in out
    assert "216" in out                      # 24 claims x 3 variants x 3 passes
    assert not (tmp_path / "out").exists() or not list((tmp_path / "out").glob("*rows*.jsonl"))
