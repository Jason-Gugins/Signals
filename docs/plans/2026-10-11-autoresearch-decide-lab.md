# Autoresearch Decide-Lab Implementation Plan

> **For the implementer:** Use the subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Build an offline, budget-bounded A/B + autoresearch loop that tunes the decide layer's three spend stages — the document gate (Jev), the bulk implementer (deepseek), and the G4 citation gate (Jev) — using the decision ledger we already collect, in the shape of TypeSafe's `autoresearch_feature_discovery` cookbook: propose questions → screen them cheaply → encode rows into numeric features → regress against outcomes → feed model errors back into the next proposal round.

**Architecture:** A new dev-only `src/lab/` package (never imported by the intel pipeline; new `signals[lab]` extra for scikit-learn) with a thin `python -m src.lab` runner. It mines the per-run `decisions.jsonl` ledgers, builds supervised targets, replays cached gate states under variant question-sets (Jev/OpenRouter calls only where a variant differs), scores variants with a weighted metric battery, and emits a calibration report proposing (never auto-applying) `config/decide.yaml` changes. All replays respect the existing data-exit consent; all rounds carry explicit token budgets and a plateau stop rule.

**Tech Stack:** Existing: TypeSafe Jev gates, OpenRouter models (haiku-5.5 planner / deepseek-v4.1-flash / glm-5.3-flash), `decisions.jsonl` ledger, `src/retrieval/embedder.py` (all-MiniLM-L6-v2, pinned, 384-dim — reused as the embedding baseline), market-profile vocabulary. New (dev-only extra `signals[lab]`): numpy + scikit-learn (elastic-net/RF/ensembles); catboost optional.

---

## Why now (the trigger this plan attacks)

Arm A of the live retrieval A/B measured the shipped G4 behavior on real traffic: **~97% of all implementer claims are routed to `reviewed`** (deepinfra 131/131, cyera 562/585 — mostly `says_nothing` at high confidence), G5 never fires, zero LLM signals promoted. The floors were shipped uncalibrated on purpose ("calibrate floors on real traffic" — TODO, decide-layer notes). This plan is that calibration program, generalized: instead of hand-tuning three gates, we build the loop that proposes, screens, A/B-tests, and regress-scores gate structures until the weighted metrics stop improving.

## Cookbook mapping (every requested item → plan component)

| Requested item | Where it lands |
|---|---|
| A/B the Document gate, Bulk implementer, G4 gate | Task 5: `variants.py` question-set registry + `replay.py` cached-state replay scorer (one variant axis per stage) |
| "Progress increases through weighted increases in output responses" | Task 5: the weighted score battery (acceptance precision, claim recall, promotion proxy, cost penalty, latency penalty) — variants advance when the weighted score improves ≥ ε |
| autoresearch loop (propose → encode → regress → errors → propose) | Tasks 6–8 wired as the round loop in `src/lab/__main__.py` |
| Pre-screen candidate questions with 4 Nouls (answerable / one-meaning / applies-to-most / varies) | Task 6: `prescreen.py` — a candidate question itself becomes the state; all four Nouls must clear before it enters a round |
| Prune correlated features | Task 6: noul-column correlation clustering across the dev corpus; keep the clearest or cheapest question per cluster |
| Simple baselines (TF-IDF, char counts, structural) | Task 4: `baselines.py` — deterministic feature family (doc length, TF-IDF vs market-profile vocabulary, existing `lexical_overlap`, quote-span lengths, heading counts); gains are only claimed over this baseline |
| Mix proposer families | Task 7: `proposers.py` — candidate batches from claude-haiku-5.5, deepseek-v4.1-flash, glm-5.3-flash (all already configured on OpenRouter), merged + deduped before screening |
| Compare model families beyond CatBoost | Task 8: `models.py` — elastic-net, random forest, SVR (RBF), gradient boosting + probability recalibration; catboost as an optional extra for the cookbook-faithful comparison |
| Embedding baseline (all-MiniLM-L6-v2 local / hosted) | Task 4: reuse `src/retrieval/embedder.py` (already shipped + pinned) — append 384-dim doc embeddings to discovered columns; measure what they carry that questions don't |
| Match validation to deployment | Task 8: primary split = chronological (runs are time-ordered; deployment predicts future runs), domain-grouped CV (no account leakage), final test set untouched by discovery AND model selection |
| Stop on a plateau | Task 8: stop when cross-validated weighted score fails to improve ≥ ε for P consecutive rounds, or the question/request budget trips |
| Longer search in agent Goal mode | Task 9: ops note — rounds are driven by `python -m src.lab run --rounds N --budget-tokens T`; an idle-time agent can own it with the same metric/budget/stop-rule contract |
| Check stability | Task 8: repeat discovery across 3 domain slices; keep questions useful in ≥ 2/3 slices |

## Current context (verified 2026-10-11)

- **The ledger is the goldmine.** Every run already persists `decisions.jsonl` beside its package: per gate row `{gate, boundary, answers (nouls/choices with probabilities), outcome, deterministic_action, model, latency_ms, raw_tokens, state_hash, agree, agree_direction, run_id}`. Arm A produced thousands of rows across 10 accounts (61/55/47 doc-gate rows; 131/380/562 G4 verdicts on the first three domains alone).
- **Claim texts are NOT persisted** — G4 rows carry `state_hash` but not the claim batch. Task 2 adds opt-in persistence (`lab.persist_claims: true` in decide.yaml → one extra `claims` field on citation rows) so future runs are replay-ready; Task 2 also backfills a bounded sample from stored docs via implementer replay (budgeted deepseek calls).
- **Doc texts are all in the raw store** (`data/raw`, content-addressed); `documents.byte_size/url/fetched_at` join by `doc_id`.
- **The embedder is live**: `src/retrieval/embedder.py`, revision-pinned, verified (384-dim) — the embedding baseline costs zero new infra and no network at inference.
- **Jev pricing** is input-only ($0.042/1M): screening + replay rounds are pennies per thousand questions; the budget risk is deepseek implementer replays (~33¢/domain observed at arm A) — hence `--budget-tokens`/--budget-$ ceilings on every round.
- **Consent posture unchanged**: replays hit TypeSafe/OpenRouter exactly as intel does; `SIGNALS_DECIDE_DATA_EXIT` gates everything as today. The lab adds no new egress class.
- **Scope guard**: `src/lab/` must never appear in the intel/watch/scheduler import graph — enforce with an import test (Task 1).

## Non-goals

- No auto-application of gate changes: the loop **proposes** `decide.yaml` edits; a human applies them (the decide layer's enforce rollout stays map-gated).
- No changes to the deterministic quote-span prefilter (binds in all modes by design).
- No play-outcome target in v1 (sparse); the calibration table becomes the v2 target when ≥30 decided plays exist per play.
- No CatBoost/scikit-learn in the production install (`[lab]` extra only, never imported by pipeline code).

---

# Tasks

### Task 0: Baseline + commit this plan into the repo

**Objective:** Record the pre-lab baseline; put this plan under version control; add the TODO entry.

**Step 1:** `pytest --collect-only -q` (record count), `git rev-parse --short HEAD`.

**Step 2:** Copy this plan to `docs/plans/2026-10-11-autoresearch-decide-lab.md` (create the dir; `.zcode/` is untracked — `docs/` is the tracked home).

**Step 3:** Add to TODO.md Deferred (or a new "Autoresearch decide-lab" section): one line pointing at the tracked plan path.

**Step 4: Commit**
```bash
git add docs/plans/2026-10-11-autoresearch-decide-lab.md TODO.md
git commit -m "docs(plan): autoresearch decide-lab — A/B + autoresearch loop for the decide gates"
```

---

### Task 1: `src/lab/` skeleton + ledger corpus extractor

**Objective:** The dev corpus: every gate row joined with its document text and run metadata.

**Files:** Create `src/lab/__init__.py`, `src/lab/corpus.py`, `src/lab/__main__.py`; test `tests/test_lab_corpus.py`. Also `tests/test_lab_isolation.py` — asserts `src.pipeline`, `src.signals`, `src.sources` never import `src.lab` (grep-based import-graph test; the scope guard).

**Corpus extractor spec** (`corpus.py::extract(db, dossiers_dir, domains=None) -> list[dict]`): walk `data/dossiers/<pkg>/decisions.jsonl` (+ `evidence.jsonl`), join `document_gate` rows to raw-store texts via `doc_id`, attach `byte_size`, `fetched_at`, `source`, and the dossier's `market_profile_id`; emit one flat dict per row. Include `raw_tokens`/`latency_ms` for cost accounting. ~80 lines, stdlib + the embedder only.

TDD: fixture package dirs under `tests/fixtures/lab/` (two tiny synthetic `decisions.jsonl` + one gzipped raw doc); tests assert row counts, the doc-text join, and domain filtering. Run: `./.venv/Scripts/python.exe -m pytest tests/test_lab_corpus.py tests/test_lab_isolation.py -v`.

**Commit:** `feat(lab): ledger corpus extractor + import-isolation guard`

---

### Task 2: Claim-text persistence (opt-in) + bounded backfill replay

**Objective:** Make G4 states replayable — the loop needs the actual claims, not just their hashes.

**Step 1 (persistence):** In `src/llm/implement.py::screen_and_gate_claims`, when config flag `lab.persist_claims` is true, serialize `[claim.text, claim.quote_span, claim.doc_id]` into the ledger row (`claims` key). Opt-in: absent/false in every shipped config. Test: flag off → rows byte-identical (extend an existing decide test file); flag on → `claims` present with exact texts.

**Step 2 (backfill):** `src/lab/backfill.py` — for sampled docs from Task 1's corpus lacking claim texts, re-run `call_implementer` (same model/prompt) to regenerate claims, cache to `data/lab/claims/<doc_id>.json`. Hard budget: `--max-docs N --budget-$ X` per invocation; cached forever after. Test with a fake implementer double.

**Commit:** `feat(lab): opt-in claim persistence + budgeted backfill replay`

---

### Task 3: Target builders

**Objective:** Supervised labels from the ledger.

**Files:** `src/lab/targets.py`, test `tests/test_lab_targets.py`.

Two targets, both domain-grouped:
1. **claim_survival** (row-level): from a citation row's final outcome — `kept` → 1, `reviewed`/`dropped` → 0. This is the abundant label (hundreds per run) and directly tunes the doc gate + implementer.
2. **doc_useful** (doc-level): did a staged doc yield ≥1 `kept` claim — the doc gate's real job. Class balance is reported per domain (honesty: if <5% positives, the report says so).

Spec the exact outcome vocabulary from `decisions.py` constants (kept/reviewed/dropped) — read `src/decide/gates.py` for the authoritative strings rather than trusting this plan.

**Commit:** `feat(lab): claim-survival and doc-usefulness target builders`

---

### Task 4: Baseline battery + embedding features

**Objective:** The bar every gate question must beat. Requires `signals[lab]` extra (`pip install -e ".[lab]"` → numpy + scikit-learn).

**Files:** `pyproject.toml` (`lab = ["numpy>=1.26", "scikit-learn>=1.5"]`), `src/lab/features.py`, `src/lab/baselines.py`, test `tests/test_lab_baselines.py`.

- Deterministic family: char/token counts, heading/list density, TF-IDF cosine vs the market profile's served-problem vocabulary (reuse `src/intel/market.py` fields), `decide_gates.lexical_overlap`, quote-span lengths, doc source one-hots.
- Embedding family: `src/retrieval/embedder.get_embedder()` on doc text (384-dim, offline once cached to `data/lab/embeddings/<doc_id>.npy`).
- `baselines.py`: elastic-net logistic on each family separately, then combined → AUROC + PR-AUC per family on a chronological split. The report line the whole plan hangs on: *"discovered questions must beat the best single family."*

TDD with tiny synthetic frames (sklearn imported lazily; tests skip if extra absent, mirroring the rerank gating pattern).

**Commit:** `feat(lab): deterministic + embedding baselines with sklearn extra`

---

### Task 5: Variant registry + weighted replay scorer (the A/B core)

**Objective:** A/B one stage at a time against recorded states.

**Files:** `src/lab/variants.py`, `src/lab/replay.py`, test `tests/test_lab_replay.py`.

- **Variant axes** (one per stage, config-shaped like their decide.yaml blocks):
  - *Document gate*: the 3 shipped Nouls vs 4-Noul sets (e.g., + `has_profile_vocab_evidence`), threshold ladders (`relevant_min` 0.45→{0.35,0.55}), question wording variants.
  - *Bulk implementer*: prompt variants (claim-count cap, stricter quote-span requirement, `--route_threshold` moves) — replayed on sampled docs (Task 2's cache; new deepseek calls only here).
  - *G4*: 3-way Choice wording, anchor-window sizes, floor/band ladders (0.70/0.30 → alternatives), says-nothing handling (review vs drop).
- **Replay**: for doc-gate/G4 variants, re-ask ONLY the variant's questions on cached texts/claims (Jev, input-only pennies); for implementer variants, bounded deepseek replays. Every replay carries `--budget-$` and writes its own `decisions.jsonl`-shaped file under `data/lab/rounds/<round>/`.
- **Weighted score** (the formalized "progress"): `score = w1·acceptance_precision + w2·claim_recall + w3·promotion_proxy − w4·cost_$ − w5·latency_s`, weights in `config/lab.yaml` (ships w1=0.4, w2=0.25, w3=0.2, w4=0.1, w5=0.05). A variant advances a round when its score beats the incumbent by ≥ ε (default 0.02).

TDD: fake decider doubles; the scorer's arithmetic is exact-asserted; budget exhaustion mid-variant is an honest `partial` result, never a crash.

**Commit:** `feat(lab): stage-variant registry + weighted replay scorer`

---

### Task 6: Question prescreener + correlation pruning

**Objective:** Never pay to A/B a bad question.

**Files:** `src/lab/prescreen.py`, test `tests/test_lab_prescreen.py`.

- Each candidate question is itself the Jev state; four zero-policy Nouls: `answerable_from_source`, `one_meaning_under_criteria`, `applies_to_most_rows`, `varies_across_rows` (cookbook battery, adapted to our row = staged doc). All four must clear their floors (config, default 0.60) on a 30-doc stratified sample before the question enters a round.
- Correlation prune: encode the dev corpus with all surviving questions' nouls → pairwise |Pearson r| > 0.85 → cluster → keep the member with the higher screening score (tie → cheaper prompt).

TDD: prescreener on fake Jev doubles (all-clear / one-fail paths); prune on synthetic correlated columns.

**Commit:** `feat(lab): 4-noul question prescreener + correlation pruning`

---

### Task 7: Multi-family proposer mixer

**Objective:** Widen the search; don't let one model's blind spots define the question space.

**Files:** `src/lab/proposers.py`, test `tests/test_lab_proposers.py`.

- Per round, each proposer family (haiku-5.5, deepseek-v4.1-flash, glm-5.3-flash — all already in `config/decide.yaml`) gets the same brief: the target definition, the current questions + their error profile (Task 8's residuals), 5 candidate questions as strict JSON.
- Merge + dedupe by normalized question text + criteria hash, then Task 6 screens them. Proposer spend is bounded per round (`proposer_budget_$`).

**Commit:** `feat(lab): multi-family proposer mixer with dedupe`

---

### Task 8: Model comparison, honest validation, plateau + stability

**Objective:** The supervised layer — and the discipline that keeps it honest.

**Files:** `src/lab/models.py`, `src/lab/splits.py`, test `tests/test_lab_models.py`.

- Models: elastic-net logistic, random forest, SVR (RBF), histogram gradient boosting (+ catboost when the optional extra exists), and isotonic/Platt recalibration when the downstream output is probabilistic (G4 probabilities).
- Splits (`splits.py`): primary = chronological by run timestamp; CV = `GroupKFold` on domain; final test = the most recent K runs, untouched by discovery and selection. Every number in the report states which split produced it.
- Loop control: plateau stop (no CV weighted-score improvement ≥ ε for P=2 rounds) and budget stop; stability = rerun proposal+screening across 3 domain slices, keep questions useful in ≥2.

**Commit:** `feat(lab): model zoo, chronological/grouped splits, plateau + stability rules`

---

### Task 9: Round runner + calibration report

**Files:** `src/lab/report.py`, `src/lab/__main__.py`, test `tests/test_lab_report.py`.

`python -m src.lab run --rounds 3 --budget-$ 1.50 --target claim_survival` executes propose → prescreen → replay → encode → regress → errors → next round, then writes `data/lab/report-<ts>.md`: per-variant weighted scores with split labels, per-question importances, baseline comparison, and a **proposed `config/decide.yaml` diff** (threshold ladders, question additions, band moves) marked PROPOSAL — application is a human step. Goal-mode note in the module docstring: an idle-time agent can own `run` given metric/budget/stop-rule.

**Commit:** `feat(lab): round runner + calibration report with propose-only decide diff`

---

### Task 10: Docs + TODO delivery record

`README.md`: a short "Decide lab (dev tooling)" section — what it is, the extra, the consent posture, that it never runs in intel. TODO.md: move the Deferred entry to a delivery record with the baseline numbers (Task 0) and the first report's headline. `pytest -q` full lane.

**Commit:** `docs: decide-lab README + TODO delivery record`

---

## Validation matrix

| Invariant | Proof |
|---|---|
| Lab never touches the pipeline | `test_lab_isolation.py` import-graph guard |
| Shipped behavior byte-identical | `lab.persist_claims` off-path test; full suite green |
| No unbounded spend | every round carries `--budget-$`; scorer treats exhaustion as honest `partial` |
| Claims are claims | deterministic quote-span prefilter untouched; replayed variants re-verify quotes first |
| Honest numbers | every reported metric names its split; baseline comparison mandatory; class balance disclosed |
| Consent posture | lab egress = same TypeSafe/OpenRouter paths gated by the same consent string |

## Risks / tradeoffs / open questions

1. **Circular labels.** v1's target (G4 survival) is the current gate's own verdict — the loop can overfit to the incumbent questions. Mitigations: the deterministic quote-span check is label-noise-anchored; baselines bound what's real; v2 swaps the target to play outcomes (revenue truth) once the calibration loop accumulates ≥30 decided plays.
2. **Corpus size.** ~10–20 runs → thousands of claim rows but dozens of docs per domain: enough for elastic-net/RF over ~40–60 features, thin for anything deeper. The plateau rule exists precisely for this.
3. **Dependency weight.** scikit-learn is heavy; it lives behind `[lab]` and is never imported by pipeline code (guarded). CatBoost stays optional.
4. **Replay drift.** Re-generated claims (backfill) may differ from the originals (sampling temperature). Mitigation: cache once and treat the cache as the state; never mix cached and fresh claims within one comparison.
5. **Cost discipline.** Implementer-variant replays are the expensive axis (~33¢/domain observed). The plan sequences doc-gate/G4 variants (Jev-pennies) first; implementer variants run on samples only.
6. **Open question for the user:** should the weighted score's `promotion_proxy` term wait until arm B lands (it currently proxies via G4 keeps, since G5 never fired in arm A)? Default: yes — w3 dormant until G5 rows exist.
