# Retrieval A/B split test — results (2026-10-13)

Control (A) = recency staging, retrieval off. Treatment (B) = hybrid
retrieval staging (FTS5 BM25 + MiniLM vectors, RRF, ms-marco rerank,
MMR), top-35 docs. Identical corpus (B ran `--skip collect`), same
models, same gates, same market profile, enforce mode. Staged sets
reconstructed by matching each arm's document-gate `state_hash` against
per-document recomputed hashes (state = doc_id + account_name + text
head-truncated to 9,600 chars).

Ledger: `tmp\ab_arms_retest.tsv` (domains: databricks.com).

## Databricks (databricks.com)

| metric | A (recency) | B (retrieval) |
|---|---|---|
| staged docs | 200 | 17 (retrieval) |
| gate excl. (of staged) | 0 | 0 |
| G4 verdicts | 3272 | 78 |
| G4 reviewed/kept/dropped | 809/0/2324 | 66/0/12 |
| G5 calls | 60 | 0 |
| Jev input tokens | 7,355,772 | 135,639 |
| Jev errored rows | 0 | 0 |

- staged overlap A∩B: 17; B-only: 0; A-only: 183
- decide_degraded: A=True B=True

## Headline totals

| total | A | B |
|---|---|---|
| G4 verdicts | 3,272 | 78 |
| reviewed | 809 | 66 |
| kept | 0 | 0 |
| dropped | 2,324 | 12 |
| Jev input tokens | 7,355,772 | 135,639 |

## Verdict

- G4 kept: A=0 vs B=0; reviewed A=809 vs B=66; dropped A=2,324 vs B=12.
- Jev input tokens: A=7,355,772 vs B=135,639 (B is 98% below A) — the cost side of the thesis.
- Compare G4-accepted and G5-promoted counts (the gates wave reports them separately) before reading `kept` as the whole outcome — the 2026-10 run showed a G5 wall behind G4. Baseline analysis for the original run: data/probe/RETRIEVAL_AB_2026_10.md.

## Post-fix context (2026-10-13, appended after the arms ran)

- **Corpus**: 220 docs (7 forced growth cycles), coverage 1.00, 40,745
  chunks. Arm A staged 200 (recency truncation bound for the first time:
  ~20 oldest docs excluded); arm B staged 17 (retrieval).
- **The fix wave's G4 changes hold at scale**: arm A accepted **139**
  claims (0 in the 2026-10 pre-fix baseline on this domain) and hit the
  G5 call cap (60) — but promoted 0 LLM signals: **the G5 band remains
  the wall** (the decide-lab's opening target).
- **Cost at scale**: arm A burned 7.36M Jev input tokens; arm B 135k
  (**−98%**) with the same promotion outcome.
- **Recall reading (honest)**: B ⊂ A again — retrieval's top-17 were all
  inside arm A's 200-newest window; none of the ~20 truncated docs were
  selected. Two readings: (a) the truncated tail is genuinely the least
  relevant content (oldest stale feed/API fetches), so retrieval correctly
  ignored it — at 9% truncation this test has low power either way; (b) a
  sharper recall test needs a bigger overflow (400+ docs) or evidence that
  old docs ARE relevant. Cost asymmetry, not recall, is now the dominant
  measured effect.
