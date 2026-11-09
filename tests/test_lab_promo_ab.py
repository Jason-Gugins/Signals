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
    SHIPPED_TEXT,
    VARIANTS,
    load_labeled_claims,
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


def _write_calibration_fixture(ws_path, sample_path):
    """Self-contained stand-in for the operator's gitignored calibration
    files (data/probe/ is operator data — absent on fresh clones/CI):
    same 24-idx schema and referee partition (4 NEED x 6 NOT x 10 CONTEXT
    x 3 GUARD x 1 ORPHAN). Duty claims carry 'stocking stuffer' so the
    FakeDecider ranks them above the NOT claims."""
    need = (4, 22, 23, 24)
    not_needs = (1, 2, 3, 5, 6, 7)
    others = (8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21)
    disposition = {}
    for i in need:
        disposition[i] = "[X] NEED"
    for i in not_needs:
        disposition[i] = "[X] NOT-A-NEED"
    for i in others:
        disposition[i] = "[X] UNSURE"
    lines = ["# synthetic promotion-gate calibration worksheet"]
    sample = []
    for i in range(1, 25):
        duty = i in need
        claim = (f"Acme markets stocking stuffers under $16 (claim {i})." if duty
                 else f"Acme ships wholesale pallets (claim {i}).")
        lines.append(f"## {i}. [promo_artifact] acme.com\n{claim}\n"
                     f"Disposition: [ ] NEED   {disposition[i]}   [ ] UNSURE")
        sample.append({"idx": i, "stratum": "promo_artifact",
                       "domain": "acme.com", "claim": claim,
                       "quote_span": claim[:40], "url": "",
                       "noul_hidden": 0.80 if duty else 0.45})
    ws_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    sample_path.write_text(json.dumps(sample), encoding="utf-8")


def test_probe_main_live_summary_computes_the_cross_regime_column(tmp_path, capsys, monkeypatch):
    """Regression: the post-scoring summary's U_cross comprehension read an
    unbound `j` in its outer condition — a latent NameError that only fires
    on the LIVE path (after all three rescore passes), never in dry-run.
    Drives the full live path (FakeDecider, zero external calls) over a
    synthetic 24-claim set with the operator's referee partition —
    self-contained because data/probe/ is gitignored operator data."""
    import src.lab.__main__ as lab_main
    import src.lab.promo_ab as pa
    from src.decide.jev import Decision

    class FakeDecider:
        model = "jev-fake"

        def decide(self, state, questions, gate=None):
            noul = 0.80 if "stocking stuffer" in state["need"] else 0.45
            return Decision(applies=True, ok=True,
                            answers={"promotion": {"type": "noul", "noul": noul}},
                            raw_tokens=4)

    monkeypatch.setattr(lab_main, "_load_decide_cfg", dict)
    monkeypatch.setattr(lab_main, "_lab_decider", lambda cfg: FakeDecider())

    ws = tmp_path / "ws.md"
    sample = tmp_path / "sample.json"
    _write_calibration_fixture(ws, sample)
    rc = pa.probe_main([
        "--worksheet", str(ws),
        "--sample", str(sample),
        "--out-dir", str(tmp_path / "out"),
        "--passes", "1",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "VERDICTS" in out and "V1 = " in out and "V2 = " in out
    summaries = list((tmp_path / "out").glob("promo-ab-*/summary.md"))
    assert len(summaries) == 1
    text = summaries[0].read_text(encoding="utf-8")
    assert "cross-regime" in text          # the U_cross column rendered
    assert "| V1 |" in text and "| V2 |" in text
    # the within-variant U column is the primary stat out of 24 pairs
    assert "/24 (sweep" in text and "/4)" in text
    # pre-registered rule v2: U is PAIRWISE (duty x NOT, out of 24, ties =
    # losses) — perfect separation means U=24 >= _U_WIN=22, so the referee
    # must clear the separation band. (The sweep-U implementation emitted
    # NO-CHANGE here — the OPERATOR-FLAGGED dead-branch bug.)
    assert "separation cleared" in text or text.count("WIN") >= 1


def test_cli_dry_run_prints_partition_and_spends_nothing(tmp_path, capsys):
    import src.lab.promo_ab as pa

    ws = tmp_path / "ws.md"
    sample = tmp_path / "sample.json"
    _write_calibration_fixture(ws, sample)
    rc = pa.probe_main([
        "--dry-run", "--worksheet", str(ws),
        "--sample", str(sample),
        "--out-dir", str(tmp_path / "out"),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert "4 NEED" in out and "6 NOT" in out and "10 CONTEXT" in out
    assert "3 GUARD" in out and "1 ORPHAN" in out
    assert "216" in out                      # 24 claims x 3 variants x 3 passes
    assert not (tmp_path / "out").exists() or not list((tmp_path / "out").glob("*rows*.jsonl"))


def test_recorded_promo_ab_verdict_reproduces_through_the_fixed_referee():
    """Verdict-fidelity pin: the RECORDED 2026-11-08 probe nouls
    (data/probe/promo-ab-20261108T001007Z/{v0,v1,v2}-rows.jsonl —
    gitignored operator data, so the values are inlined here and CI must
    pass without the files), re-refereed through the FIXED pairwise-U
    instrument, reproduce the recorded verdict: V1 WIN (U = 24/24, exact
    p = 0.0048, all four legs) and V2 PARTIAL (separation cleared, duty
    lift failed: 0.314 < 0.353). Values are rescore's round(mean, 4) —
    transcribed verbatim; duty_lift == 0.052 depends on them exactly."""
    from src.lab.promo_ab import referee_verdict

    # v{n}_noul per idx 1..24, verbatim from the recorded rows files
    v0_nouls = {1: 0.68, 2: 0.2833, 3: 0.5367, 4: 0.3467,
                5: 0.12, 6: 0.2567, 7: 0.2533, 8: 0.12,
                9: 0.22, 10: 0.1367, 11: 0.2733, 12: 0.2833,
                13: 0.3033, 14: 0.26, 15: 0.4233, 16: 0.1967,
                17: 0.2067, 18: 0.25, 19: 0.23, 20: 0.1933,
                21: 0.2733, 22: 0.28, 23: 0.47, 24: 0.3167}
    v1_nouls = {1: 0.08, 2: 0.0733, 3: 0.08, 4: 0.4767,
                5: 0.0867, 6: 0.0733, 7: 0.1367, 8: 0.16,
                9: 0.49, 10: 0.21, 11: 0.3233, 12: 0.5333,
                13: 0.3867, 14: 0.44, 15: 0.7367, 16: 0.48,
                17: 0.5267, 18: 0.4633, 19: 0.4567, 20: 0.4467,
                21: 0.4633, 22: 0.2733, 23: 0.4333, 24: 0.4367}
    v2_nouls = {1: 0.05, 2: 0.05, 3: 0.05, 4: 0.31,
                5: 0.0567, 6: 0.0467, 7: 0.0767, 8: 0.1433,
                9: 0.12, 10: 0.1467, 11: 0.1433, 12: 0.1767,
                13: 0.1233, 14: 0.17, 15: 0.2767, 16: 0.14,
                17: 0.13, 18: 0.2, 19: 0.1433, 20: 0.1567,
                21: 0.1867, 22: 0.1633, 23: 0.46, 24: 0.3233}
    # by_pass in the summary loop's shape: vid -> idx -> row
    by_pass = {"V0": {i: {"v0_noul": v} for i, v in v0_nouls.items()},
               "V1": {i: {"v1_noul": v} for i, v in v1_nouls.items()},
               "V2": {i: {"v2_noul": v} for i, v in v2_nouls.items()}}
    v0_by_idx = by_pass["V0"]
    duty_mean_v0 = sum(v0_by_idx[i]["v0_noul"] for i in (4, 22, 23, 24)) / 4
    v1 = v2 = None
    for vid in ("V1", "V2"):
        by_idx = by_pass[vid]
        key = f"{vid.lower()}_noul"
        duty_mean = sum(by_idx[i][key] for i in (4, 22, 23, 24)) / 4
        promo_max = max(by_idx[i][key] for i in (1, 3))
        ctx_max = max(by_idx[i][key]
                      for i in (9, 10, 14, 15, 16, 17, 18, 19, 20, 21))
        guard_max = max(by_idx[i][key] for i in (11, 12, 13))
        # the fixed pairwise U — the summary loop's comprehension exactly
        u_pair = sum(1 for i in (4, 22, 23, 24) for j in (1, 2, 3, 5, 6, 7)
                     if by_idx[i][key] > by_idx[j][key])
        v = referee_verdict(U=u_pair, promo_max=promo_max, ctx_max=ctx_max,
                            duty_mean=duty_mean,
                            duty_mean_v0_rebaseline=duty_mean_v0,
                            guard_max=guard_max)
        if vid == "V1":
            v1 = v
        else:
            v2 = v

    # Recorded 2026-11-08 verdict (data/probe/promo-ab-20261108T001007Z),
    # now reproduced by the instrument rather than by hand:
    assert v1["verdict"].startswith("WIN")          # U_pair=24/24, p=0.0048,
    assert v1["duty_lift"] == 0.052                 # 0.405 - 0.353; legs: suppressed,
                                                    # guard_ok, lifted all True
    assert v2["verdict"].startswith("PARTIAL")      # duty lift failed (0.314 < 0.353)
