# A/B split-test tooling (retrieval posture)

Promoted from the untracked 2026-10 experiment in `tmp/` (`ab_phase*.sh`,
`ab_final_report.py`, `ab_arms.tsv`, `ab_backup_20261010/`). `tmp/` is
experiment history — **never delete or rewrite it**; this directory is the
reusable version.

- `run_split_test.sh` — run one arm over a list of domains (resume-aware)
- `report.py` — reconstruct both arms' staged sets and produce the comparison
- Baseline results: `data/probe/RETRIEVAL_AB_2026_10.md`

## The two-arm protocol

| | arm A (control) | arm B (treatment) |
|---|---|---|
| staging | recency (retrieval off) | hybrid retrieval (FTS5 BM25 + MiniLM vectors, RRF, rerank, MMR) |
| config pre-step | `retrieval.enabled: false` | `retrieval.enabled: true` + built index (`signals index`) |
| collect | runs, cadence-gated | **`--skip collect` (frozen corpus)** |
| decide mode | `SIGNALS_DECIDE_MODE=enforce` (env-locked by the script) | same |
| LLM layer | `--with-llm` | same |

Everything else (models, gates, `--market-profile ab_ai_infra_gtm`) is held
identical between arms; the ONLY differences are the retrieval posture and
arm B's skipped collect.

### Arm A's manual pre-step (there is no env override)

`src/core/config.py::_apply_env_overrides` has **no `SIGNALS_*` hook for the
retrieval block** (verified 2026-10), so the posture is set by editing
`config/default.yaml`:

```yaml
retrieval:
  enabled: false   # arm A — flip back to true before arm B
```

The runner prints a grep-based reminder of the current value but does NOT
enforce it — verify by hand before trusting an arm. This also means the
script is agnostic to the repo's shipped default (whatever it is this week):
you always set the posture explicitly per arm.

### Order of operations (per experiment)

1. Build the corpus for every domain first (`intel <domain> --force` cycles
   or natural cadence) — both arms must judge the same documents.
2. Flip `retrieval.enabled: false`, run **arm A** (its cadence-gated collect
   is a no-op right after the build).
3. Flip `retrieval.enabled: true`, build the index (`signals index --domain
   <domain>` per domain; check coverage with `signals index --stats`), run
   **arm B** — it passes `--skip collect` itself, enforcing the frozen-corpus
   rule: B never refetches, so any doc difference between the arms is
   attributable to staging, not to corpus drift.
4. Generate the report.

## Running an arm

```bash
./scripts/ab/run_split_test.sh A databricks.com rillet.com        # retrieval-off arm
./scripts/ab/run_split_test.sh B databricks.com rillet.com        # retrieval-on arm (--skip collect)
```

Knobs (env): `AB_LEDGER` (default `tmp/ab_arms.tsv`), `AB_MARKET_PROFILE`
(default `ab_ai_infra_gtm`), `SIGNALS_PY` (default the repo venv python).

- No `--name` is passed on purpose: `run_intel` keeps each account's
  existing DB name, and the report's state-hash reconstruction joins on that
  name — passing a conflicting `--name` would rename the account and break
  the join.
- Per-domain logs: `data/logs/ab/arm<A|B>_<domain>.log`; run log:
  `data/logs/ab/run.log`. A failed domain is ledgered with its exit code
  and does not stop the rest (delete its ledger row to retry it).

## Ledger format

Tab-separated, one row per arm×domain, append-only:

```
<arm>\t<domain>\t<dossier-package-dirname>\trc=<exit code>
```

Resume rule: the runner skips any arm+domain pair already present. To re-run
a domain, back up the ledger (see below) and drop the row.

## Report

```bash
./.venv/Scripts/python.exe scripts/ab/report.py \
  --ledger tmp/ab_arms.tsv --out data/probe/RETRIEVAL_AB_<date>.md \
  databricks.com
```

No arguments reproduces the 2026-10 defaults (the 10-domain corpus, the
experiment ledger) — **careful: the default `--out` is the committed
2026-10 record**; pass `--out` for anything new. Staged sets are
reconstructed via the document-gate `state_hash` (doc_id + account_name +
9,600-char text head) — the proven method; domains need both arms ledgered
and are skipped (with a notice) otherwise.

## The >200-doc re-test (staged protocol — not auto-run)

The original experiment's oldest-doc recall thesis was under-exercised: at
72 docs/domain the recency tail (200 newest) already covers the corpus.
databricks.com is the candidate closest to the threshold. Gated on an
explicit go:

1. **Grow** databricks.com past 200 stored docs — repeated
   `intel databricks.com --force` cycles (observed ~40 docs/cycle ⇒ ~3–4
   cycles) or a week of natural cadence.
2. **Verify**: `./.venv/Scripts/python.exe -m src.cli index --stats` must
   show `domain=databricks.com docs=2xx ... coverage>=0.80` (documents
   > 200 AND chunk coverage ≥ 0.8). Rebuild the index after growth
   (`signals index --domain databricks.com`).
3. **Run both arms** (posture flips per the protocol above):
   `run_split_test.sh A databricks.com` then `run_split_test.sh B databricks.com`.
4. **Report**: `report.py --out data/probe/RETRIEVAL_AB_RETEST_<date>.md databricks.com`.

**Budget:** measured 2026-10 rates were ~$0.39/domain for the recency arm
and ~$0.20/domain for the retrieval arm at ~40–70 docs; the recency arm
scales with corpus (token spend covers every staged doc), so at >200 docs
expect ~$1.50–2 for databricks arm A plus ~$0.20–0.50 for arm B — plan
≈ **$2 total**.

## Backups (`tmp/ab_backup_*`)

Before re-using or resetting a ledger/log set, copy it aside following the
existing convention — `tmp/ab_backup_20261010/` is the pattern:

```bash
mkdir -p tmp/ab_backup_$(date +%Y%m%d)
cp tmp/ab_arms.tsv data/logs/ab/run.log tmp/ab_backup_$(date +%Y%m%d)/
```

`tmp/` stays untracked history: nothing there is ever deleted by this
tooling (the runner appends to the ledger in place — that is why the backup
step comes first).
