# G5 Sibling Fix Implementation Plan

> **For the implementer:** Use the subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Reproduce the G4 wall fix on G5: diagnose whether G5's promotion nouls (0.08–0.13 on all 49 post-fix accepted claims) are caused by the same paraphrase-anchored excerpt defect, fix `intel.py`'s G5 evidence construction to anchor on the claim's verified quote span, and validate with a live harvest — without touching any thresholds.

**Architecture:** A four-task wave. Task 1 is an OFFLINE diagnosis over the existing validation-harvest packages (zero spend): recompute what `anchor_window(cited_text, need_text)` actually served to Jev for the 49 G5-judged claims and measure anchor rates. Task 2 is the fix — confined to the G5 loop in `src/pipeline/intel.py` (`gate_need_promotion` takes the excerpt as a plain string; the decide layer is untouched), reusing G4's proven `claim_excerpt` helper. Task 3 is the live validation harvest on the same two domains. Task 4 is cleanup + docs. A STOP gate sits between Tasks 1 and 2: if the diagnosis shows anchor rates are already HIGH (bug theory wrong), the wave stops and hands the evidence to the decide-lab — no fix, no threshold tuning.

**Tech Stack:** Existing only. `decide_gates.claim_excerpt` (G4's helper, gates.py:225), `anchor_window`, the candidates↔pairs index alignment (already load-bearing for the amounts threading), pytest. No new dependencies.

---

## Verified context (2026-10-12 — trust these over re-derivation)

- **The bug site.** `src/pipeline/intel.py:858-871`: the G5 loop iterates `for cand in candidates:` and builds
  ```python
  need_text = cand.summary or cand.title or ""
  cited_text = doc_text_by_id.get(str((cand.evidence_data or {}).get("doc_id") or ""))
  evidence_excerpt = decide_gates.anchor_window(cited_text, need_text) if cited_text else need_text
  ```
  `need_text` is a PARAPHRASE (the candidate's summary/title). `anchor_window` needs a verbatim normalized substring — the G4 study measured 88–97% of paraphrases don't match, so Jev receives the ~400-char document head (its own docstring: "frequently nav/boilerplate").
- **The fix helper exists.** `decide_gates.claim_excerpt(doc_text, claim)` (gates.py:225) — quote-anchored with claim-text fallback, shipped in the fix wave and already exercised by the G4 replay tests. It takes the doc text and the `Claim` (which carries `.quote_span`).
- **The claim is reachable.** `claims_to_candidates` (implement.py:616) builds one candidate per `(claim, meta)` pair **in order** — the amounts threading at intel.py:844-853 already relies on `candidates[idx] ↔ pairs[idx]`. So in the G5 loop, `pairs[idx][0].quote_span` is the candidate's verified quote. `Claim(text, doc_id, quote_span=…)` is constructed at implement.py:342.
- **The wall data.** Fix-wave validation harvest: databricks 19 G4-accepted → 19 G5 calls → all dropped, nouls 0.08–0.13; rillet 30 accepted → 30 G5 calls (5 pre-fix) → dropped, same noul range. `band_low: 0.30`. Packages: `databricks.com-20261012T190544714929Z-0a338a47`, `rillet.com-20261012T190947430792Z-6a965119` — their citation rows carry persisted claims (`lab.persist_claims` was ON during the harvest).
- **`gate_need_promotion(need_text, evidence_excerpt, deterministic_promote, decider, gate_cfg, ledger, run_id, mode)`** (gates.py:1163) — the excerpt is a plain argument. The decide layer needs ZERO changes.
- **Environment:** credentials + consent live in `.env`; the pipeline loads them (proven across every harvest). Study profile `ab_ai_infra_gtm` must be temporarily re-added for harvest runs (removed in cleanup). `MAX_G5_CALLS = 60` (intel.py constant) — the harvest must stay under it (49 accepted last time; if the fix pushes accepts past 60, G5 capping kicks in honestly — report it as such).

## STOP gate

Task 1 measures, for every G4-accepted claim that reached G5: does `need_text` anchor in the cited doc (Jev's actual excerpt = head fallback?), and does `quote_span` anchor (the fix's anchor)? **If need_text anchors ≥ ~70%** (bug theory wrong — Jev saw real context), STOP: write the probe doc with the negative result, skip Tasks 2–3, and hand the noul distribution to the decide-lab as its G5 evidence. The wave's whole premise is falsifiable in one offline script.

---

# Tasks

### Task 0: Baseline + tracked plan + TODO

**Steps:** `pytest --collect-only -q` (record); `git rev-parse --short HEAD`; copy this plan to `docs/plans/2026-10-12-g5-sibling-fix.md`; TODO Deferred entry ("G5 sibling fix — quote-anchored promotion excerpt; plan: <path>").
**Commit:** `docs(plan): G5 sibling fix — quote-anchored promotion excerpt`

### Task 1: Offline G5 diagnosis (zero spend — parent-executed)

**Objective:** Measure, for the 49 G5-judged claims, what Jev actually saw.

**Method (single python script, `tmp/g5_study.py`, output to `data/probe/G5_WALL_2026_10.md` after the STOP-gate check):** walk the two validation packages' `decisions.jsonl`; collect `need_promotion` rows (noul, outcome) joined with their accepted claims from the citation rows (persisted `claims`: text/quote_span/doc_id — the accepted claims ARE the G5 inputs; `need_text` is the candidate summary derived from claim text — measure BOTH claim.text anchoring and quote anchoring via `anchor_window`'s own normalization); report per domain: n, need_text-anchor rate, quote-anchor rate, noul distribution vs anchor status. Close-read: 6 sample rows showing the head Jev would have seen vs the quote-bearing excerpt the fix would serve.

**STOP gate** (as above). If proceeding:

**Commit:** `docs(probe): G5 wall diagnosis — <anchor rates> (sibling-bug confirmed/refuted)`

### Task 2: The fix — quote-anchored G5 excerpt

**Files:** Modify `src/pipeline/intel.py` (G5 loop only), test in `tests/test_intel_llm_layer.py` (new test functions at end of file).

**Step 1: Failing test.** In the FakeOrchLlm bootstrap (mirror `test_g5_judges_cited_document_text_not_claim_echo` — it already exists and pins G5 judging the CITED doc; extend the pattern): a claim whose `quote_span` sits MID-DOCUMENT (past the 400-char head, the fix-wave's own RED-phase shape) with a summary paraphrase that does not appear verbatim ⇒ spy/patched decider captures G5's `evidence_excerpt` argument ⇒ assert it CONTAINS the quote span (and is not the bare head). Fallback pins: claim without quote_span ⇒ current `anchor_window(cited_text, need_text)` behavior; doc not in scope ⇒ `need_text` as today.

**Step 2: Implement.** In the G5 loop:
```python
for cand_idx, cand in enumerate(candidates):
    ...
    claim = pairs[cand_idx][0] if cand_idx < len(pairs) else None
    cited_text = doc_text_by_id.get(...)
    if cited_text and claim is not None and claim.quote_span:
        # G5 sibling of the G4 wall fix: judge the promotion against the
        # excerpt anchored on the claim's VERIFIED quote span — the
        # paraphrased need_text almost never verbatim-anchors (G5_WALL
        # study), which served Jev a boilerplate head instead of context.
        evidence_excerpt = decide_gates.claim_excerpt(cited_text, claim)
    elif cited_text:
        evidence_excerpt = decide_gates.anchor_window(cited_text, need_text)
    else:
        evidence_excerpt = need_text
```
Edge cases pinned by tests: `cand_idx >= len(pairs)` (defensive — keeps today's behavior); claim with blank `quote_span` ⇒ `claim_excerpt`'s own claim-text fallback; G5 cap interaction unchanged (`MAX_G5_CALLS` logic untouched).

**Step 3: Verify.** New tests green; the full decide/intel lane green UNMODIFIED: `pytest tests/test_intel_llm_layer.py tests/test_decide_gates.py tests/test_claim_persistence.py tests/test_intel_retrieval_staging.py -q`.

**Step 4: Commit.** `fix(intel): G5 promotion excerpt anchors on the verified quote span (G4 sibling fix)`

### Task 3: Validation harvest (live, ~$0.50)

**Steps:** temporarily re-add `ab_ai_infra_gtm` to `config/markets.yaml` (the Task 9 cleanup removes it); `lab.persist_claims: true` (diagnosis data for the G5 rows' inputs); `SIGNALS_DECIDE_MODE=enforce` + `--with-llm --skip collect`, same two domains (databricks.com, rillet.com). Then measure vs the pre-fix harvest (Task 1's baseline): **G5 noul distribution (pre 0.08–0.13 → post ?), G5 outcomes (dropped/reviewed/promoted), LLM signals in dossier** — the decision-grade metric is whether ANY claim promotes (noul ≥ 0.30) or the band still binds with BETTER evidence. Append the before/after table to `data/probe/G5_WALL_2026_10.md`; commit.

**Interpretation contract (decided now, no post-hoc wiggling):** nouls rise materially (>0.2 median) ⇒ sibling bug confirmed as the cause; promotions still 0 ⇒ the band (0.30) is genuinely above what this evidence supports ⇒ hand `band_low` to the decide-lab as a PROPOSAL (not applied). Nouls stay ~0.1 ⇒ the needs are genuinely weak; record and stop.

**Commit:** `docs(probe): G5 sibling fix validation — nouls <pre> -> <post>, promoted <n>`

### Task 4: Cleanup + docs

Restore `lab.persist_claims: false`; remove the temporary profile; README gates-table G5 row gains the quote-anchored-excerpt note (mirror the G4 row's phrasing); TODO delivery record (anchors measured, fix, validation numbers, spend).
**Commit:** `docs: G5 sibling fix — delivery record; restore study flags`

---

## Validation matrix

| Invariant | Proof |
|---|---|
| No-fix path untouched | off-branch (no quote/no cited doc) fallbacks pinned by tests; decide layer unchanged |
| The fix does what it claims | spy-decider test: G5's captured excerpt contains the quote span |
| Stop-gate honesty | Task 1 runs BEFORE Task 2; negative result stops the wave |
| Validation honesty | per-claim noul distribution + G5-accepted-vs-promoted reported separately; interpretation contract fixed in Task 3 |
| Suite | ~2,650+ tests, offline lane green |

## Risks

1. **`need_text` ≠ claim.text.** The candidate summary is derived from the claim but may rephrase; the diagnosis measures claim.text anchoring as the proxy and the close-read checks real summaries. If summaries diverge wildly, the fix still helps (quote anchor is exact) but the measured pre-fix anchor rate understates the problem — noted in the probe doc.
2. **Nouls may stay low even with perfect context** — possible: the promoted "needs" genuinely aren't strong. The interpretation contract handles it: that outcome redirects effort to claim QUALITY (decide-lab) rather than band-lowering.
3. **G5 cap (60)** may bind if accepts grow past 60 across the two domains — capped candidates are unpromoted (narrow-only); the report states the cap was hit.
4. **Spend:** ~$0.50 total (two enforce runs at the measured ~$0.20–0.25/domain retrieval-arm rates).
