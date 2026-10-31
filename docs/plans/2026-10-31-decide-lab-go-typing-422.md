# Decide-Lab GO + Taxonomy Typing 422 — Operation Plan (2026-10-31)

> **For the implementer:** Use the subagent-driven-development skill for code tasks; Tasks A1 and B2 are parent-executed by design (live-API diagnosis and ledger arithmetic with cost attribution).

**Goal:** Execute the two authorized budget-gated operations: (A) the taxonomy-typing 422 fix — diagnosis → reshape → first-ever successful harvest; (B) the decide-lab calibration go — add the missing need_promotion variant axis, run budget-capped rounds over the live ledger, and produce a PROPOSED `config/decide.yaml` diff for the three named levers (need_promotion floor 0.70 / band_low 0.30 / output_screen 0.35/0.70).

**Authorization:** explicit user go 2026-10-31 for both streams, with these hard ceilings: **A1 ≤ $0.05** (penny-bisect), **B1 ≤ 300 lab calls** (Jev input-only ≈ pennies; proposers bounded by the runner), **A3 ≤ $1.00** (validation harvest). Total night ceiling **≤ $5** — if any step's measured cost shape exceeds its ceiling, STOP that stream and record.

**Standing contract (from the decide-lab's own non-goals):** the lab **proposes** `decide.yaml` edits; application is a human step. Tonight produces the proposal + evidence, NOT auto-applied thresholds. Same for A2: no threshold/granularity smuggling — routing semantics unchanged.

---

## Verified context (2026-10-31 — trust these)

- **The lab is shipped and runnable**: `src/lab/` complete; `python -m src.lab run --rounds N --budget-calls T --target {claim_survival,doc_useful} [--domains ...] [--dry-run]`; DB opened READ-ONLY. BUT `variants.py` axes are **document_gate + g4 (floor ladders 0.60/0.80, band_low alternates, output_screen ladders)** — **no `need_promotion` axis exists**, and `grep need_promotion src/lab/` is empty. The band/floor levers the user named live in `config/decide.yaml:33` (`need_promotion: {floor: 0.70, band_low: 0.30}`) and `:35` (`output_screen: {review_threshold: 0.35, action_threshold: 0.70}`).
- **The corpus is ready**: last night's diagnosis (`data/probe/G5_SIBLING_DIAGNOSIS_20261030.md`) joined **114 need_promotion rows by exact state_hash, 0 unmatched**, over 120 unique persisted claims (50 old + 70 new); **9 claims in-band (0.30–0.34)** from `rillet.com-20261030T234923165282Z-6129cabe` (Ashby prose). Nouls are RECORDED in `data/dossiers/<pkg>/decisions.jsonl` — re-thresholding under floor/band ladders is **offline arithmetic, zero LLM calls**.
- **decide.yaml shipped values** (the calibration incumbent): `citation_soundness {floor:0.70, band_low:0.30}`, `need_promotion {floor:0.70, band_low:0.30}`, `output_screen {review:0.35, action:0.70}`, `document_gate {relevant_min:0.45, evidence_min:0.55}`.
- **The 422 battery**: 3,323/3,323 taxonomy_typing rows errored history-wide; the 422 response BODY was never read (cause field holds only the generic httpx string); measured payload: 54 criteria / 2,493 bytes, 7 categories largest 13; the proposed_category fallback already exists (gates.py routing 0.90/2.0/0.60).
- **Keys present**: `.env` carries `TYPESAFE_API_KEY` + `SIGNALS_DECIDE_DATA_EXIT` (checked presence only).
- **Concurrent session ACTIVE** (npm/reddit community-signals wave: `510f789`, `d059874`, `0ced316`). Standing git safety: stage by explicit path only; never touch `BUILD_NOTES.md`, `config/markets.yaml`, `docs/server-architecture-exploration.*` or any file this wave didn't author; re-check `git status` before every commit. Live harvests (`A3`) serialize with any concurrent run writing dossiers.
- **Budget reality**: Jev is input-only ($0.042/1M) — G4/doc-gate replays are pennies; the expensive axis (deepseek implementer replays, ~33¢/domain) is NOT in tonight's scope (no implementer variants); proposer calls are the real spend and are runner-bounded.

## STOP gates

- **A1 (422 diagnosis)**: bisect inconclusive after ~10 requests ⇒ write the probe with the matrix, STOP stream A (battery stays honestly degraded), hand to the lab. Do not guess-fix.
- **B (lab)**: plateau rule may stop rounds early — honest. The lab NEVER applies decide.yaml changes.
- **Either stream**: measured cost exceeds its ceiling ⇒ STOP that stream, record actuals.

---

# Stream A — Taxonomy typing 422 (docs/plans/2026-10-13-taxonomy-typing-422.md)

### A1. Parent-executed penny-bisect diagnosis (≤ $0.05)

Follow the 422 plan's Task 1 exactly: offline payload reconstruction from a persisted rillet package + real Taxonomy; live bisect (1q×54 → 1q×27 → 1q×13 → 1q×7 → 2q×54), capturing `exc.response.text[:300]` on every 422; docs check against TypeSafe's reference; write `data/probe/TYPING_422_20261031.md` with the matrix + bodies + fix-shape decision (H/S/C/X).
**Commit:** `docs(probe): taxonomy typing 422 — root cause <finding>, fix shape <H/S/C/X>`

### A2. Ship the fix per the diagnosis (TDD, offline)

Per the 422 plan's Task 2, adjusted to the actual finding (Shape H assumed): `classify_claims` two-phase (category Choice ≤7 criteria → type Choice ≤13 for confident claims), signature/return contract unchanged, granularity routing untouched; `jev.py` cause enrichment (`exc.response.text[:160]` on HTTPStatusError — permanent); typing pre-flight allowance ×2 if two-phase. RED tests first: request-shape bounds, routing pins green-unmodified, cause-enrichment respx test, 30-claim bound regression.
**Verify:** decide tests + full offline gate. **Commit:** `fix(decide): taxonomy typing reshaped per 422 diagnosis — <shape>; Jev causes carry response bodies`

### A3. Validation harvest (live, ≤ $1.00)

`lab.persist_claims: true` + `ab_ai_infra_gtm` temporarily (same two domains, `--with-llm --skip collect`, enforce). Assertions: taxonomy_typing errored == **0** (first in history); typed proposals > 0; `decide_degraded` clears if typing was the sole error source. Flags restored after. Serialize with any concurrent dossier-writing run.
**Commit:** `docs(probe): taxonomy typing validated — 0 errored, <n> claims typed`

### A4. Cleanup + TODO delivery record
**Commit:** `docs: taxonomy typing 422 fix — delivery record`

---

# Stream B — Decide-lab calibration go

### B0. Add the `need_promotion` variant axis (TDD, offline — the missing lever)

**Files:** `src/lab/variants.py`, `src/lab/replay.py`; test `tests/test_lab_replay.py` (extend).

**Spec:** new variant family `g5` over the `need_promotion` gate: re-threshold the RECORDED nouls (no re-ask — the noul is already in the row) under ladder variants: `floor` {0.70 incumbent, 0.60, 0.50} × `band_low` {0.30 incumbent, 0.20, 0.40} + one `output_screen` cross variant. Semantics per decide.yaml:32-33 + `src/decide/gates.py` (read the authoritative noul routing: noul ≥ floor ⇒ promote; band_low ≤ noul < floor ⇒ review; below band ⇒ drop — VERIFY against the real code, code wins). Each variant re-routes the 114 recorded rows and reports promote/review/drop counts + the promotion precision proxy (promoted claims whose G4 outcome was kept). The 9 in-band claims are the canary: a variant that DROPS them must say so in its rationale. Config deltas stay config-shaped (`{"floor": 0.60}` etc.) so the report's propose-diff machinery applies unchanged.

**Verify:** `pytest tests/test_lab_replay.py tests/test_lab_isolation.py -v`; full offline gate.
**Commit:** `feat(lab): need_promotion re-threshold axis — floor/band ladders over recorded nouls`

### B1. Run the calibration rounds (budget-capped, propose-only)

1. `python -m src.lab run --dry-run` — record the plan, zero calls.
2. `python -m src.lab run --rounds 2 --budget-calls 300 --target claim_survival` — doc-gate + g4 + new g5 axes + proposer mixer; plateau rule may stop early. Actual call/token spend recorded from the report.
3. The report lands at `data/lab/report-<ts>.md`: per-variant weighted scores (split-labeled), baseline comparison, and the **PROPOSED decide.yaml diff** — including the g5 ladder verdict (does any floor/band alternative beat the incumbent on the weighted score, and what happens to the 9 in-band claims under it).

### B2. Parent read of the G5 ladder (offline arithmetic, the 9-claim canary)

Cross-check the lab's g5 numbers with a direct script over the 114 rows (`tmp/g5_ladder.py`): for each (floor, band) pair — promote/review/drop counts, how many of the 9 in-band Ashby claims promote/review/drop, and precision-proxy where labels allow. This is the evidence the morning recommendation hangs on.
**Record:** append the ladder table to `data/probe/G5_SIBLING_DIAGNOSIS_20261030.md` (or a new `data/probe/DECIDE_LAB_GO_20261031.md`).

### B3. Records

TODO: flip the decide-lab operation entry to delivered (rounds run, spend actuals, proposal path); the PROPOSED diff is presented to the user — NOT applied. Night ceiling accounting in the final handoff.
**Commit:** `docs: decide-lab go — calibration rounds run, proposal recorded`

---

## Validation matrix

| Invariant | Proof |
|---|---|
| No unbounded spend | A1/A3 dollar ceilings; B1 `--budget-calls 300`; per-step actuals recorded |
| The lab proposes, never applies | report's PROPOSED diff untouched in config/decide.yaml (git diff proves zero config commits from stream B) |
| g5 axis is arithmetic, not invention | re-threshold tests pin routing against the REAL gates.py semantics; 9-claim canary named in every variant rationale |
| First-ever typing success | A3: 0 errored taxonomy_typing rows, typed > 0 |
| No threshold smuggling in A2 | routing tests (0.90/2.0/0.60) green unmodified |
| Concurrent-session safety | per-commit `git status` re-check; only own files staged |

## Risks

1. **The 422 root cause may be weirder than criteria count** — the bisect matrix + body capture decides; STOP gate prevents blind iteration.
2. **Two-phase typing doubles typing requests** — pennies; pre-flight allowance updated honestly.
3. **The g5 ladder's precision proxy is thin** (promoted claims have no ground-truth outcome until plays decide) — the report labels it a proxy, the canary (9 in-band claims) is the concrete read.
4. **Round-loop output may be modest** (corpus ~120 claims) — the plateau rule exists for exactly this; a null result is a result and gets recorded.
