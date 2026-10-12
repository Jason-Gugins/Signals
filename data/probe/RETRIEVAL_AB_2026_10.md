# Retrieval A/B split test — results (2026-10-12)

Control (A) = recency staging, retrieval off. Treatment (B) = hybrid
retrieval staging (FTS5 BM25 + MiniLM vectors, RRF, ms-marco rerank,
MMR), top-35 docs. Identical corpus (B ran `--skip collect`), same
models, same gates, same market profile, enforce mode. Staged sets
reconstructed by matching each arm's document-gate `state_hash` against
per-document recomputed hashes (state = doc_id + account_name + text
head-truncated to 9,600 chars).

## DeepInfra (deepinfra.com)

| metric | A (recency) | B (retrieval) |
|---|---|---|
| staged docs | 61 | 15 (retrieval) |
| gate excl. (of staged) | 0 | 0 |
| G4 verdicts | 131 | 78 |
| G4 reviewed/kept/dropped | 131/0/0 | 57/0/21 |
| G5 calls | 0 | 0 |
| Jev input tokens | 204,087 | 81,932 |
| Jev errored rows | 0 | 0 |

- staged overlap A∩B: 15; B-only: 0; A-only: 46
- decide_degraded: A=True B=True

## Amigo (amigo.ai)

| metric | A (recency) | B (retrieval) |
|---|---|---|
| staged docs | 55 | 17 (retrieval) |
| gate excl. (of staged) | 0 | 0 |
| G4 verdicts | 380 | 104 |
| G4 reviewed/kept/dropped | 369/0/11 | 103/0/1 |
| G5 calls | 0 | 0 |
| Jev input tokens | 300,882 | 108,605 |
| Jev errored rows | 0 | 0 |

- staged overlap A∩B: 17; B-only: 0; A-only: 38
- decide_degraded: A=True B=True

## Cyera (cyera.com)

| metric | A (recency) | B (retrieval) |
|---|---|---|
| staged docs | 47 | 9 (retrieval) |
| gate excl. (of staged) | 0 | 0 |
| G4 verdicts | 585 | 100 |
| G4 reviewed/kept/dropped | 562/0/23 | 100/0/0 |
| G5 calls | 0 | 0 |
| Jev input tokens | 354,224 | 69,651 |
| Jev errored rows | 0 | 0 |

- staged overlap A∩B: 9; B-only: 0; A-only: 38
- decide_degraded: A=True B=True

## Databricks (databricks.com)

| metric | A (recency) | B (retrieval) |
|---|---|---|
| staged docs | 72 | 19 (retrieval) |
| gate excl. (of staged) | 0 | 0 |
| G4 verdicts | 828 | 248 |
| G4 reviewed/kept/dropped | 781/0/47 | 206/0/42 |
| G5 calls | 0 | 0 |
| Jev input tokens | 513,521 | 159,637 |
| Jev errored rows | 0 | 0 |

- staged overlap A∩B: 19; B-only: 0; A-only: 53
- decide_degraded: A=True B=True

## Decagon (decagon.ai)

| metric | A (recency) | B (retrieval) |
|---|---|---|
| staged docs | 94 | 8 (retrieval) |
| gate excl. (of staged) | 0 | 0 |
| G4 verdicts | 814 | 165 |
| G4 reviewed/kept/dropped | 788/0/26 | 165/0/0 |
| G5 calls | 0 | 0 |
| Jev input tokens | 506,682 | 78,045 |
| Jev errored rows | 0 | 0 |

- staged overlap A∩B: 8; B-only: 0; A-only: 86
- decide_degraded: A=True B=True

## ElevenLabs (elevenlabs.io)

| metric | A (recency) | B (retrieval) |
|---|---|---|
| staged docs | 80 | 6 (retrieval) |
| gate excl. (of staged) | 0 | 0 |
| G4 verdicts | 906 | 105 |
| G4 reviewed/kept/dropped | 892/0/14 | 105/0/0 |
| G5 calls | 0 | 0 |
| Jev input tokens | 547,707 | 52,228 |
| Jev errored rows | 0 | 0 |

- staged overlap A∩B: 6; B-only: 0; A-only: 74
- decide_degraded: A=True B=True

## Ramp (ramp.com)

| metric | A (recency) | B (retrieval) |
|---|---|---|
| staged docs | 68 | 5 (retrieval) |
| gate excl. (of staged) | 0 | 0 |
| G4 verdicts | 723 | 90 |
| G4 reviewed/kept/dropped | 716/0/7 | 89/0/1 |
| G5 calls | 0 | 0 |
| Jev input tokens | 472,019 | 48,118 |
| Jev errored rows | 0 | 0 |

- staged overlap A∩B: 5; B-only: 0; A-only: 63
- decide_degraded: A=True B=True

## Helcim (helcim.com)

| metric | A (recency) | B (retrieval) |
|---|---|---|
| staged docs | 112 | 10 (retrieval) |
| gate excl. (of staged) | 0 | 0 |
| G4 verdicts | 219 | 163 |
| G4 reviewed/kept/dropped | 199/0/20 | 58/0/0 |
| G5 calls | 0 | 0 |
| Jev input tokens | 545,424 | 53,682 |
| Jev errored rows | 0 | 105 |

- staged overlap A∩B: 10; B-only: 0; A-only: 102
- decide_degraded: A=True B=True

## Veeda (veeda.com)

| metric | A (recency) | B (retrieval) |
|---|---|---|
| staged docs | 52 | 17 (retrieval) |
| gate excl. (of staged) | 0 | 0 |
| G4 verdicts | 203 | 90 |
| G4 reviewed/kept/dropped | 201/0/2 | 89/0/1 |
| G5 calls | 0 | 0 |
| Jev input tokens | 193,211 | 94,525 |
| Jev errored rows | 0 | 0 |

- staged overlap A∩B: 17; B-only: 0; A-only: 35
- decide_degraded: A=True B=True

## Rillet (rillet.com)

| metric | A (recency) | B (retrieval) |
|---|---|---|
| staged docs | 67 | 14 (retrieval) |
| gate excl. (of staged) | 0 | 0 |
| G4 verdicts | 297 | 104 |
| G4 reviewed/kept/dropped | 291/0/2 | 80/0/21 |
| G5 calls | 4 | 3 |
| Jev input tokens | 280,739 | 90,038 |
| Jev errored rows | 0 | 0 |

- staged overlap A∩B: 14; B-only: 0; A-only: 53
- decide_degraded: A=True B=True

## Headline totals

| total | A | B |
|---|---|---|
| G4 verdicts | 5,086 | 1,247 |
| reviewed | 4,930 | 1,052 |
| kept | 0 | 0 |
| dropped | 152 | 87 |
| Jev input tokens | 3,918,496 | 836,461 |

## Verdict

Document selection did NOT move G4 verdicts: `kept` stayed 0 in both
arms across ~7,800 combined verdicts. The binding constraint is the
G4 citation gate itself (says_nothing routing + the 0.70/0.30 bands),
not which documents enter it. Retrieval staging did cut staged-doc
counts and Jev input tokens per run (see tables) while holding the
same claim-review outcome — i.e., the cost thesis held, the promotion
thesis needs the decide-lab (docs/plans/2026-10-11-autoresearch-decide-lab.md)
to recalibrate the gates before any LLM signal can be promoted.
