# Taxonomy Typing 422 Fix Implementation Plan

> **For the implementer:** Use the subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Fix the taxonomy typing battery, which has NEVER succeeded — 3,323 errored rows across all history, zero successes — by diagnosing the exact 422 rejection from TypeSafe's own response body, then reshaping the request per the finding, validated by a live harvest with zero typing errors.

**Architecture:** Three tasks. Task 1 is the parent-executed diagnosis: reconstruct a real typing payload offline (done — numbers below), then a live penny-bisect (~$0.02) that captures the 422 RESPONSE BODY (never read before — the current cause field holds only the generic httpx string) and bisects the criteria count to find whether the limit is per-question or per-request, cross-checked against TypeSafe's documented limits. Task 2 ships the fix the diagnosis dictates (leading shape: hierarchical category→type Choice, which composes with the battery's existing category-fallback semantics) plus a permanent improvement: Jev error causes include the HTTP response body snippet. Task 3 validates on two live domains: **zero** taxonomy_typing errored rows (the battery has literally never had one). Task 4 cleans up and records.

**Tech Stack:** Existing only — TypeSafe Jev (`src/decide/jev.py`), `classify_claims` (`src/decide/gates.py:1830`), the real Taxonomy (54 entries, 7 categories), respx for offline request tests. No new dependencies.

---

## Verified context (measured 2026-10-13 — trust these over the code's own comments)

- **3,323 taxonomy_typing rows across ALL history: 100% errored.** Zero successes in any package, any run, any doc size. This is a birth defect, not a size regression — every "well under the ~240 Choice bound" belief in the docstring (gates.py:1855) is untested belief.
- **The measured request**: one batched request per doc-with-accepted-claims; state `{"claim_<i>": anchor_window(doc_text, claim.text)}`; ONE Choice question per accepted claim; criteria = all 54 taxonomy entries as `f"{key}: {label} ({category})"`. Measured: criteria total **2,493 bytes**, longest entry **74 bytes**, one claim question **2,807 bytes JSON**, a 10-claim doc request ≈ **36 KB total**. None of these are large by API standards.
- **The taxonomy has 7 categories**: financial (12), hiring (8), intent (4), negative (5), neutral (3), operational (13), technology (9). A two-stage category→type shape is structurally available (largest stage = 13 criteria).
- **The 422 body was never read.** `LiveDecider` raises `HTTPStatusError` and the Task 5 cause field captured `f"{type(exc).__name__}: {str(exc)[:120]}"` — the generic httpx line. The response body (which for a 422 almost certainly names the violated constraint verbatim) is on `exc.response` and was discarded.
- **Evidence pattern**: every doc-with-accepted-claims errors (30/30 rillet, 19/19 databricks, 3323/3323 history-wide), independent of claim count or doc size ⇒ structural/schema rejection, not size. G4's batched requests (up to ~20 questions × 3 criteria) succeed on the same decider ⇒ the difference is in the typing question's shape (54 criteria is the obvious outlier).
- **Typing granularity is IN CODE** (gates.py:1859+): per-claim reading of the Choice answer with `confident` 0.90 / `separation_min` 2.0 / `ambiguous` 0.60 routing to `proposed_type` vs `proposed_category`. The proposed_category fallback ALREADY exists — a hierarchical fix composes with shipped semantics.
- **Config**: `decider.timeout_s: 5.0`, `per_question_s: 0.5` (scales), breaker excludes routing/posture_audit. typing cfg: `confident/ambiguous` keys.

## STOP gate

Task 1's live bisect either (a) identifies a concrete rejected dimension (criteria count / schema field / size) with the 422 body confirming it, or (b) doesn't. On (b): STOP — write the probe doc with everything measured (including the bisect matrix), leave the battery as-is (it is already honestly degraded), and hand the mystery to the decide-lab. Do not guess-fix a working-otherwise API integration.

---

# Tasks

### Task 0: Baseline + tracked plan + TODO

**Steps:** `pytest --collect-only -q` (record); `git rev-parse --short HEAD`; copy this plan to `docs/plans/2026-10-13-taxonomy-typing-422.md`; TODO Deferred entry ("Taxonomy typing 422 — battery never succeeded (3,323/3,323 errored history-wide); diagnosis → reshape → validate").
**Commit:** `docs(plan): taxonomy typing 422 — diagnose, reshape, validate`

### Task 1: Diagnosis — read the 422 body and bisect the request (parent-executed, ~$0.02)

**Method:** a probe script (`tmp/typing422_probe.py`) using `LiveDecider` + respx-free real calls against the REAL Typesafe endpoint, with `SIGNALS_DECIDE_DATA_EXIT` + `TYPESAFE_API_KEY` from `.env`:

1. **Reconstruct the real payload offline first** (no network): build `classify_claims`'s exact state+questions for one real rillet doc (persisted accepted claims in `rillet.com-20261029T2034…/decisions.jsonl` + the real Taxonomy via `Taxonomy.load("config/signals.yaml")`). Record: n_questions, total bytes, per-question bytes, criteria count.
2. **Capture the 422 body**: send the reconstructed request via `decider.decide(...)` wrapped so the HTTPStatusError's `exc.response.text[:300]` is printed (the probe does this directly with httpx — bypass Decision for visibility). Expected: the body names the violated constraint.
3. **Bisect** (each step one penny request, body captured):
   - 1 question × 54 criteria ⇒ 422? (if 200: the limit is per-REQUEST — go to step 4)
   - 1 question × 27 criteria (half the entries) ⇒ ?
   - 1 question × 13 criteria (one category) ⇒ ?
   - 1 question × 7 criteria (one category-list) ⇒ ?
   - 2 questions × 54 criteria ⇒ ? (per-request accumulation test)
   - If a 1-question × 54 request returns 200: the bug is NOT the criteria — inspect the difference vs the failing full request (state keys? question count?) and re-bisect on THAT axis.
4. **Docs check**: fetch TypeSafe's API reference (docs.typesafe.ai — the cookbook host already referenced in TODO.md) for stated per-question criteria / options limits; reconcile with the bisect result.
5. Write `data/probe/TYPING_422_20261013.md`: the payload stats, the bisect matrix with bodies, the root cause, and the **fix-shape decision**:
   - **Shape H (hierarchical)** if criteria-count-limited: `classify_claims` becomes two-phase — phase 1: one Choice per claim over the 7 categories; phase 2: for claims whose top category prob ≥ `ambiguous`, one Choice per claim over that category's types (≤13 criteria). Both phases under any plausible bound; the existing `proposed_type`/`proposed_category`/`separation` routing applies UNCHANGED at the type phase. Requests: 1 per doc per phase (2× the typing requests — still pennies; within `decider.max_decide_tokens_per_run` since the typing allowance in intel.py's pre-flight must be updated to 2× `_TYPING_TOKEN_ALLOWANCE`).
   - **Shape S (slim)** if criteria-CONTENT-limited: criteria shrink to `key: label` (drop category parens) or key-only + the categories listed once in the instructions.
   - **Shape C (chunk)** if request-size-limited: claims chunked into sub-batches per request.
   - **Shape X (schema)** if the body names a field: fix that field.
**STOP gate:** bisect inconclusive after ~10 requests ⇒ write the probe with the matrix, stop, decide-lab handoff.

**Commit:** `docs(probe): taxonomy typing 422 — root cause <finding>, fix shape <H/S/C/X>`

### Task 2: The fix per the diagnosis

**Files (Shape H assumed; adjust per Task 1's finding):** `src/decide/gates.py` (classify_claims two-phase), `src/decide/jev.py` (cause enrichment: on HTTPStatusError, `cause` includes `exc.response.text[:160]` — permanent, makes every future 4xx/5xx readable), `src/pipeline/intel.py` (typing pre-flight allowance ×2 if Shape H doubles requests), `config/decide.yaml` (only if a new knob is genuinely needed — prefer not), `tests/test_decide_gates.py` + `tests/test_decide_jev.py`.

**Spec (Shape H):** `classify_claims` keeps its signature and return contract (`dict[int, dict]` keyed by claim index, `proposed_type`/`proposed_category`/`confidence`/`separation`). Internals: phase-1 request (category Choice per claim, 7 criteria), phase-2 request (type Choice per claim with a confident category, ≤13 criteria); granularity routing (confident 0.90 / separation 2.0 / ambiguous 0.60) applied at the type phase exactly as today; claims without a confident category get `proposed_category` from phase 1 alone (that is the shipped fallback semantics, now reached via two bounded requests). Ledger rows: one per claim as today, with a `phase` note; errored rows carry the (now body-enriched) cause.

**Tests:** request-shape tests assert BOTH phases' criteria ≤ the diagnosed bound and the category list matches the real Taxonomy categories; granularity routing unchanged (existing tests updated only where they pinned the single-request shape — say so); jev cause-enrichment test (respx 422 with a body ⇒ Decision.cause contains the body snippet); a chunk/bound regression test (a 30-claim doc produces per-phase requests within the bound).

**Verify:** new + existing decide tests green; the offline payload reconstruction (Task 1 script re-run) shows both phases within bounds.
**Commit:** `fix(decide): taxonomy typing reshaped per 422 diagnosis — <shape>; Jev causes carry response bodies`

### Task 3: Validation harvest (live, ~$0.50)

`lab.persist_claims: true` + `ab_ai_infra_gtm` temporarily re-added (same two domains, `--with-llm --skip collect`, enforce). **Decision-grade assertions:** taxonomy_typing errored rows == **0** (first in history); typed proposals > 0 (`claims_typed` count in the stage result); `decide_degraded` clears if typing was the sole error source. Append the before/after to the probe doc (3,323-history baseline vs post-fix). Restore flags after.
**Commit:** `docs(probe): taxonomy typing validated — 0 errored, <n> claims typed`

### Task 4: Cleanup + docs + TODO

Flags restored; README decide-layer notes gain one line (typing is two-phase, why); TODO delivery record (the never-worked revelation, root cause, validation numbers).
**Commit:** `docs: taxonomy typing 422 fix — delivery record`

---

## Validation matrix

| Invariant | Proof |
|---|---|
| Root cause is measured, not guessed | Task 1 bisect matrix + 422 bodies in the probe doc |
| No threshold/granularity change smuggled in | routing tests (confident/separation/ambiguous) green unmodified |
| Requests stay within the diagnosed bound | request-shape tests assert per-phase criteria ≤ bound; 30-claim regression test |
| First-ever success | Task 3: zero errored typing rows, typed proposals > 0 |
| Future 4xx/5xx readable | Jev cause carries response body snippet (test-pinned) |

## Risks

1. **TypeSafe's limit may be undocumented or weirder than criteria-count** — the bisect matrix is the fallback evidence; Shape X covers named-field rejections. The STOP gate prevents blind iteration.
2. **Two-phase typing doubles typing requests** (pennies; the pre-flight allowance updates to match — an honest, disclosed envelope change).
3. **Hierarchical routing may shift proposal quality** (category-then-type vs 54-way single shot) — the separation/confident semantics are unchanged, and the validation harvest reports `proposed_type` vs `proposed_category` counts for the record.
4. **The 422 body may reveal something outside the typing battery** (e.g., an account-level schema constraint) — then the probe doc records it and the wave stops at the STOP gate, with the finding.
