# A/B Follow-up Fix Wave Implementation Plan

> **For the implementer:** Use the subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Convert the retrieval A/B's findings (`data/probe/RETRIEVAL_AB_2026_10.md`) into fixes: diagnose and repair the G4 promotion wall (the experiment's headline failure), make retrieval the default staging posture (proven −75–79% load at identical outcomes), fix the three operational defects the experiment exposed (Jev fixed-timeout error losses, whole-plan invalidation on one bad planner step, embedder OOM), and stage the >200-doc re-test that actually exercises old-doc recall.

**Architecture:** Eight mostly-independent fixes over the existing decide/retrieval layers — one diagnostic study gate (Task 2) that DECIDES which of two pre-specified G4 fixes ships in Task 3, config/default-posture flips with their test/doc pins, and two small library fixes (jev.py timeout scaling + error detail, embedder batching). A final operational task promotes the A/B tooling from untracked `tmp/` into tracked `scripts/` and stages the corpus-growth re-test.

**Tech Stack:** Existing only — no new dependencies. TypeSafe Jev, OpenRouter (deepseek/haiku/glm), SQLite, pytest.

---

## Verified context (pre-resolved 2026-10-12 — trust these over guesses)

- **The G4 routing rule** (`src/decide/gates.py:788-792, 968-975`): per-claim 3-way Choice (supports/contradicted/says_nothing); `prob >= floor` routes the TOP label — supports ⇒ kept, contradicted ⇒ dropped, **says_nothing ⇒ reviewed**; `prob < floor` ⇒ reviewed. The A/B measured: top label is `says_nothing` at high confidence for ~90–97% of claims, both arms, 0 kept across ~6,300 verdicts.
- **The implementer prompt** (`src/llm/implement.py:103-117`): already demands verbatim ≤300-char quotes and doc-only grounding. Claims therefore PASS the deterministic quote-span check yet still get judged says_nothing — the leading hypothesis is deepseek emits *inferential/summary* claims whose quote only partially supports the assertion.
- **Jev errors: the timeout theory is DEAD; the breaker cascade is the proven cause.** Ledger replay across all 2,005 timed A/B decide calls: max latency **1,230 ms** — the fixed 5.0s timeout (`src/decide/jev.py:126,165`) never fired once. The real mechanism (helcim arm B, verifiable in its decisions.jsonl): ~4 fast failures (~80–150 ms, no retry signature ⇒ 4xx/bad-shape, in the *minor* gates routing + posture_audit — chronic in every domain, both arms) push the ratio over `error_rate_breaker: 0.20` (`src/decide/jev.py:190-193`, BREAKER_MIN_CALLS=5); the breaker then fast-fails EVERY subsequent call — ~6 G4 batches = 105 claim-rows lost, plus completeness/routing. Also: all 240 errored rows carry the literal `"jev_error"` — `Decision` has no cause field and `LiveDecider` swallows the exception (`src/decide/shapes.py:29-32`, `jev.py:179-188`). And the `decide.yaml:21` comment claiming the timeout "scales with questions-per-request" is a comment-code lie (harmless hygiene to fix, not a behavior fix).
- **THE EXCERPT-MISS MECHANISM (new, data-implicated, neither original branch named it).** `gate_citation_batch` builds Jev's excerpt as `anchor_window(doc_text, claim.text)` (`src/decide/gates.py:896`) — anchoring on the *paraphrased claim text*. When the paraphrase isn't a verbatim doc substring (the normal case), `find` fails and Jev judges against a ~400-char document HEAD (`HEAD_TOKENS=100`, gates.py:126,207) whose own docstring calls it "frequently nav/boilerplate". The **verified quote — the one span guaranteed present, already checked at gates.py:884 — never enters the Jev state.** This cleanly explains 97.6% says_nothing at median top-prob 0.96 (databricks A: 801 says_nothing vs 20 supports) and the frequent `out_of_excerpt` screen fires.
- **The "reviewed" population is heterogeneous.** Databricks arm A's 781 reviewed rows split: `output_review` 412 (batch output screen — which OVERRRODE 17 supports-labeled claims), `says_nothing` 308, `review_label_prob` 61. Any study that samples "reviewed" without stratifying by reason mixes three mechanisms.
- **A second wall waits behind G4.** Rillet's 4 G4-ACCEPTED claims died at G5 with promotion nouls 0.11–0.14 vs `band_low` 0.30. A G4 fix that works will still read "kept=0" unless the harvest reports G4-accepted counts separately from G5 promotions.
- **The G4 levers I originally proposed are behaviorally inert.** `says_nothing⇒dropped` is a ledger relabel only — enforce takes `outcome=="accepted"` (gates.py:1030-1035), reviewed claims never landed anyway (review is calibration bookkeeping per README:304-311). The floor 0.70→0.60 affects only supports-labeled sub-floor claims (3 of 828 in databricks A) — a says_nothing top label routes to review on BOTH sides of the floor (gates.py:971-979). Real Branch-G levers must change ROUTING or the excerpt, not the floor.
- **Planner: one bad step kills the whole plan — via TWO paths.** `src/llm/planner.py:194-199` — a proposed step adding a fanout source with `include_fanout` false ⇒ `generate_plan` returns None; **and `planner.py:191-193` does the same for any invented source id** (`unknown_source:sid`). Both models trip one or the other (mimo: warn_notices, haiku: sec_formd), so the planner has NEVER successfully narrowed a plan in any run we've seen. Fix must prune BOTH: strip disallowed source_ids from the step, drop the step if empty, keep the plan (narrow-only).
- **Retrieval posture today:** `config/default.yaml` ships `retrieval.enabled: false`; `tests/test_retrieval_config.py::test_retrieval_config_defaults_off` pins it; README's "Hybrid retrieval staging (opt-in)" section documents the opt-in path; `tests/test_doctor_retrieval.py` has a disabled-default branch. `candidate_limit: 60` is the real staging selector (60 chunks ≈ 5–19 docs at 5–15 chunks/doc — `stage_docs: 35` almost never binds); also pinned in the config test.
- **Embedder OOM:** `src/retrieval/embedder.py::OnnxEmbedder.embed()` encodes the whole batch in one `session.run` — the databricks monster doc (~2.6k chunks) requested a 174 GB arena allocation; the indexer caught it (`errors: 1`), databricks stuck at 71/72 covered.
- **Claim texts are not persisted** — G4 rows carry `state_hash` only (proven during the A/B's staged-set reconstruction). The diagnosis study needs claims, so this wave ships the opt-in persistence the decide-lab plan already specced as its Task 2 (dedupe note below).
- **A/B tooling is untracked** (`tmp/ab_phase*.sh`, `tmp/ab_compare.py`, `tmp/ab_final_report.py`, `tmp/ab_arms.tsv`, `tmp/ab_backup_20261010/`). Task 8 promotes the reusable parts to `scripts/ab/`.
- Baseline: ~2,643 tests collected, offline lane green. Configs are at pre-experiment state; `config/markets.yaml` carries the user's own uncommitted edit — **never touch or stage it**.

## Decision gate up front (resolves the plan's open questions at execution time)

Task 2's study decides Task 3's branch, with the excerpt-miss mechanism as a named third candidate:
- **Branch E — excerpt construction (new, data-implicated):** Jev never sees the verified quote. Fix = build the G4 excerpt anchored on the QUOTE span (window around it), claim text as context — not the reverse. This is a gates.py excerpt-construction change, testable deterministically.
- **Branch I — implementer flavor:** deepseek emits assertions beyond what the doc states. Fix = prompt tightening in `build_extraction_messages` (+ `tests/test_llm_implement.py:155-168` prompt pin). Note: if Branch I "works," it likely works *through* anchorability (restatements anchor) — the study must record anchorability so the real mechanism is visible.
- **Branch G — gate calibration (recalibrated):** the inert levers are dropped (says_nothing⇒dropped is a relabel; the floor move touches 3/828 claims). Live levers: the batch output screen (`output_review` 412 of 781 reviewed in databricks A, overriding 17 supports — review_threshold/action_threshold in decide.yaml) and, if G4 accepts start flowing, G5's `band_low: 0.30` vs observed promotion nouls 0.11–0.14 (rillet).
- The study's split decides the order; Branch E ships first if excerpt-miss dominates (it is also the cheapest and most deterministic to fix and verify).

---

# Tasks

### Task 0: Baseline + tracked plan + TODO

**Steps:** record `pytest --collect-only -q` count + `git rev-parse --short HEAD`; copy this plan to `docs/plans/2026-10-12-ab-followup-fix-wave.md`; add a TODO entry ("A/B follow-up fix wave — G4 wall diagnosis, retrieval default-on, jev/planner/embedder fixes") pointing at it.
**Commit:** `docs(plan): A/B follow-up fix wave — G4 wall, default-on, operational fixes`

### Task 1: Opt-in claim persistence (ported from decide-lab Task 2, Step 1)

**Files (corrected seam per review — the citation rows are written in gates.py, not implement.py):** Modify `src/decide/gates.py` (`gate_citation_batch` — attaches the claims payload to its rows), `src/llm/implement.py` (`screen_and_gate_claims` — new `persist_claims: bool = False` param, threaded into `gate_citation_batch`), `src/pipeline/intel.py` (compute `persist_claims = bool((decide_cfg.get("lab") or {}).get("persist_claims"))` where `decide_cfg` IS in scope, ~line 683-687, and pass it at BOTH call sites: the `_per_doc` path ~722-732 AND the five-fields path ~953-963), `config/decide.yaml` (new top-level `lab:` block: `persist_claims: false`); test `tests/test_claim_persistence.py`.

**Spec:** when the flag is on, each citation_soundness ledger row (dropped/error/reviewed/accepted — ALL the rows `gate_citation_batch` writes, gates.py:847-860, 940-955, 1017+) gains `claims: [{"text","quote_span","doc_id"}, …]` for the batch it judged. Off/absent ⇒ rows byte-identical (pin with a serialization diff test). `DecideLedger.record` appends verbatim — enrichment happens where the rows are BUILT, never post-hoc.
**Commit:** `feat(decide): opt-in claim persistence in G4 ledger rows`

### Task 2: The G4 wall study (the decision gate — parent-executed, not subagent)

**Protocol (de-confounded per review):**
1. Harvest: with `persist_claims: true`, retrieval ON, enforce mode, re-run `intel --with-llm --skip collect` on **two** domains — databricks.com (richest G4 volume: 828) and rillet.com (the only domain with G4 accepts: 4, all killed at G5). Budget ≈ $0.50.
2. Sample **stratified by review reason** — `output_review` / `says_nothing` / `review_label_prob` separately (~20/~20/~10 across the two domains; do NOT pool them): three different mechanisms, three different fixes.
3. For EVERY sampled claim, record mechanically (all derivable from the persisted claim + raw store): (a) does `claim.text` anchor in the doc (`anchor_window` hit vs head-fallback)? (b) does `quote_span` anchor? (c) quote present (already deterministically verified — always true)? Then the human label: `doc-grounded` / `inferential` / `wrong-entity` / `excerpt-miss` (claim grounded in the doc, but the excerpt Jev saw was the boilerplate head — checkable by reconstructing the state Jev received).
4. Write `data/probe/G4_WALL_2026_10.md`: the stratified table, anchor rates per stratum, the branch split, and the **disposition** (Branch E / I / G order). Commit it (`docs(probe): G4 wall diagnosis — <split>`).

### Task 3: Ship the fix(es) the study chose, in its order

**Branch E (excerpt construction — ships first if excerpt-miss dominates):** in `gate_citation_batch`, build the Jev excerpt as a window anchored on the VERIFIED QUOTE span (quote present by construction), with the claim text as labeled context; keep the anchor-window-on-claim as fallback only when no quote exists. Deterministically testable: given doc + claim + quote, assert the excerpt contains the quote span. Update the routing pins in `tests/test_decide_gates.py` only if routing legitimately changes.
**Branch I (implementer prompt):** in `build_extraction_messages`, add the explicit contract — claims must be restatements of statements the document explicitly makes; if the document only implies it, do not emit; prefer fewer fully-grounded claims. Update `tests/test_llm_implement.py:155-168` (the prompt pin).
**Branch G (recalibrated levers only):** the output screen thresholds (`output_screen.review_threshold/action_threshold` — the mechanism behind 412 of 781 reviewed in databricks A, including 17 overridden supports) and, once G4 accepts flow, G5 `band_low` (0.30 vs observed promotion nouls 0.11–0.14). The floor move and the says_nothing⇒dropped relabel are OUT (inert — see context).
**Validation harvest (all branches):** re-run the same 2 domains (~$0.50), join claims across runs via `claim_natural_key` (`src/llm/implement.py:547-556` — idempotent text hash) and report PER-CLAIM VERDICT FLIPS (McNemar-style), plus — critically — **G4-accepted counts separately from G5 promotions**, so a G4 fix that works isn't misread as failure behind the G5 wall. n=50 decides the branch order only; calibration constants wait for the decide-lab's larger corpus. Append results to the probe doc.
**Commit:** `fix(decide): G4 wall — <branches> per data/probe/G4_WALL_2026_10.md`

### Task 4: Retrieval default-on + candidate_limit 180

**Files:** `config/default.yaml` (`retrieval.enabled: true`; `candidate_limit: 60` → `180`, comment updated: "chunks per leg; the real staging selector — 60 under-selected at 5–19 docs"), `tests/test_retrieval_config.py` (exactly ONE assertion flips — `enabled is False` at line 27; the 180 pin is ADDED to the defaults test — `candidate_limit` was never pinned there; the missing-block-still-off test at lines 71-80 is unaffected), `src/pipeline/health.py` (the disabled-branch OK string "retrieval disabled … (default)" at ~line 438 becomes false post-flip — update wording; the WARN branches unchanged), `tests/test_doctor_retrieval.py` (comment/docstring touch only — every test sets `enabled` explicitly, nothing mechanical flips), `tests/test_intel_llm_layer.py` (ADDED per review: FakeOrchLlm runs `run_intel(with_llm=True)` on a bare `Config()` — post-flip these exercise the retrieval/uncovered path and gain gap lines; make the affected assertions gap-tolerant or set an explicit disabled config in those tests), `README.md` (retrieval section: default posture now ON with the `signals index` prerequisite; note the honest `uncovered` gap line + doctor WARN for fresh installs without an index).

**Posture note baked into the README:** with retrieval on and no index, `--with-llm` runs stage recency + a gap line naming `signals index` — that is the intended onboarding signal, not an error.
**Commit:** `feat(retrieval): default-on posture + candidate_limit 180 (A/B-validated)`

### Task 5: Jev honesty + breaker recalibration (timeout demoted to hygiene)

The proven failure chain (see context): ~4 fast 4xx/bad-shape failures in minor gates → `error_rate_breaker: 0.20` trips at min 5 calls → the rest of the run fast-fails. The timeout NEVER fired (max observed latency 1.23s of 5.0s).

**Files:** `src/decide/shapes.py` (Decision gains a `cause: str | None = None` field), `src/decide/jev.py` (LiveDecider populates `cause` from the caught exception; breaker becomes config-aware), `src/decide/gates.py` + `src/decide/alignment.py` (the ~11 `"jev_error"` write sites emit `decision.cause` when present, `"jev_error"` otherwise), `config/decide.yaml` (new decider knobs, honestly commented), `tests/test_decide_jev.py` + the gates tests.

**Spec:**
1. **Honest error rows** (ships the diagnosis instrument): every errored ledger row carries `f"{type(exc).__name__}: {str(exc)[:120]}"` once Decision.cause flows. The NEXT run's evidence becomes conclusive.
2. **Breaker recalibration**: new decider config `breaker: {min_calls: 5, threshold: 0.20, exclude_gates: [routing, posture_audit]}` — chronic single-RTT failures in the minor gates must not fail-close G4/completeness for the whole run. Default keeps today's trip math EXCEPT the exclusion list (which changes shipped behavior — that is the point, justified by the ledger evidence; call it out in the commit).
3. **Timeout scaling as hygiene**: implement the comment's promise (`effective = timeout_s + per_question_s × max(0, n−3)`, knob `per_question_s: 0.5`) or delete the comment — implementer's choice, noted in the commit; it fixes a lie, not an observed failure.
4. **Same honesty at the implementer seam**: `call_implementer`'s failure path (`src/llm/implement.py:262-267`) logs but swallows the cause into "five-fields extraction failed" gaps (13 of 40 A/B dossiers) — thread the exception summary into the returned failure so the gap line names it.
**Tests:** cause-field propagation; breaker exclusion math (4 routing errors + N G4 successes ⇒ no trip; 4 citation errors ⇒ trip); write-site emission; timeout formula unit test; implementer-cause threading with a fake client.
**Commit:** `fix(decide): real error causes in ledger; breaker excludes chronic minor-gate failures; timeout comment made true`

### Task 6: Planner — prune disallowed steps, never invalidate the plan

**Files:** `src/llm/planner.py` (~194-195), `tests/test_llm_planner.py`.

**Spec:** BOTH whole-plan-kill paths get step-level pruning: (a) fanout sources in a step when `include_fanout` is false ⇒ remove those source_ids from the step; (b) invented source ids (`unknown_source:sid`, planner.py:191-193) ⇒ same treatment — strip the invented ids, and if the step becomes empty, drop the step; the plan survives with its remaining steps (narrow-only: one bad step must not cost the whole plan — both planner models trip one path or the other on every run today). Log a warning naming the pruned step and ids.
**Tests:** a plan with one fanout-carrying step + two clean steps ⇒ plan returns 2 steps, warning recorded; an invented-id step ⇒ ids stripped (step survives if it keeps valid sources, dropped if empty); `include_fanout=True` passes fanout steps through; existing fanout pin at `tests/test_llm_planner.py:176-188` updated.
**Commit:** `fix(planner): prune disallowed/invented step sources instead of invalidating the whole plan`

### Task 7: Embedder batch cap (the 174 GB OOM)

**Files:** `src/retrieval/embedder.py`, `tests/test_retrieval_embedder.py`.

**Spec:** `EMBED_BATCH = 32` module constant; `embed()` slices into batches, concatenates results in order; per-batch failure still degrades to None for the whole call (existing contract). Test: FakeSession counts `run()` calls — 100 texts ⇒ 4 calls, results in order; an OOM-raising fake on batch 2 ⇒ None overall.
**Commit:** `fix(retrieval): batch embedder inference (32/batch) — kills the 174GB arena OOM`

### Task 8: A/B tooling promoted + the >200-doc re-test staged

**Files:** Create `scripts/ab/README.md`, move-adapt `tmp/ab_phase2_armA.sh`+`tmp/ab_phase3_armB.sh` into one `scripts/ab/run_split_test.sh` (parameterized domain list + ledger path), `tmp/ab_final_report.py` → `scripts/ab/report.py` (paths parameterized), `tmp/ab_backup` convention documented. Delete nothing from `tmp/` (history lives there).

**The staged re-test (documented in the README, not auto-run):** pick databricks.com (72 docs, closest to 200); grow via repeated `intel databricks.com --force` cycles (≈3–4 cycles at the observed ~40 docs/cycle) or a week of natural cadence; verify `documents > 200 AND document_chunks coverage ≥ 0.8`; then `run_split_test.sh databricks.com` and `report.py`. This is the test that actually exercises old-doc recall — the original retrieval thesis. Budget note: ≈$0.60 at arm-B-only rates; the recency arm at 200+ docs costs ~$1.50–2.
**Commit:** `feat(scripts): promote A/B split-test tooling to scripts/ab + >200-doc re-test protocol`

### Task 9: Docs + delivery record

README (retrieval section already touched in Task 4; add the fix-wave outcomes), TODO delivery record: what shipped per task, the G4 study's stratified split + post-fix harvest numbers (G4-accepted AND G5-promoted, per-claim flips), spend, and the decide-lab note — **this wave satisfied Step 1 only of the decide-lab plan's Task 2 (claim persistence); annotate Step 1, keep Step 2 (backfill) standing.**
**Commit:** `docs: A/B follow-up fix wave — delivery record`

---

## Validation matrix

| Invariant | Proof |
|---|---|
| Off-path byte-identity where it still applies | persist_claims off ⇒ ledger rows byte-identical (Task 1 test); retrieval absent-block still off (Task 4 test) |
| No unbounded spend | harvests are 2-domain, ~$0.50 each, gated on Task 2/3 flow; no other live calls |
| Narrow-only survives | planner prune test (fanout can never survive into a plan when excluded) |
| Honest degradation everywhere | embedder batch-failure ⇒ None; jev error rows carry causes; on_error fallbacks untouched |
| Suite | baseline ~2,643 + ~20 new tests, offline lane green |

## Risks / open questions

1. **Branch E is the leading hypothesis, not a proven one** — the excerpt-miss mechanism fits the data (97.6% says_nothing, anchor-on-paraphrase, quote-never-shown) but the study's per-claim anchor-rate measurements are what prove it. If anchor rates come back HIGH in the says_nothing stratum, Branch E is wrong and Branch I/G ordering takes over — the stratification exists precisely to prevent wishful routing.
2. **Branch I prompt fix may not move the needle** — deepseek may keep emitting inferential claims; the per-claim flip join (claim_natural_key) is the honest check. If nothing moves, that is a recorded finding for the decide-lab, not a failure of the wave.
3. **The breaker exclusion changes shipped behavior** (routing/posture errors no longer count toward the trip) — justified by the ledger evidence, but if the chronic 4xx errors turn out to be real outages-in-disguise, the exclusion delays a legitimate fail-close. Task 5's honest-error rows make the next run's evidence readable either way.
4. **Default-on + no index ⇒ gap line + doctor WARN on every `--with-llm` run/fresh install** until `signals index` runs — intended onboarding signal (README'd in Task 4), noisy for existing users; the TODO record says so plainly.
5. **G5 band calibration is deferred until G4 accepts flow** — Branch G's G5 lever (band_low vs observed nouls 0.11–0.14) only becomes measurable after a G4 fix ships; the harvest reports it but this wave does not tune it (that's the decide-lab's job with the larger corpus).
6. **>200-doc re-test is operational, not code** — it spends real budget and days of cadence; it ships staged, gated on your go.
