#!/usr/bin/env bash
# run_split_test.sh — run ONE arm of the retrieval A/B split test.
#
# Adapted from tmp/ab_phase2_armA.sh + tmp/ab_phase3_armB.sh (2026-10
# experiment; the originals stay in tmp/ as history — never delete them).
#
# Usage:
#   run_split_test.sh <arm> <domain> [more domains...]
#     arm A — retrieval OFF (recency staging control).  MANUAL PRE-STEP:
#             set retrieval.enabled: false in config/default.yaml first.
#             There is NO SIGNALS_* env override for the retrieval block
#             (see src/core/config.py::_apply_env_overrides), so the flip
#             is a config edit, made by you, before the run.
#     arm B — retrieval ON (hybrid staging treatment). MANUAL PRE-STEP:
#             retrieval.enabled: true AND a built index (./"$PY" -m
#             src.cli index --domain <domain>). Arm B ALWAYS passes
#             `--skip collect` (frozen-corpus rule: B must judge exactly
#             the corpus A judged — see README.md).
#
# Both arms run `--with-llm` under SIGNALS_DECIDE_MODE=enforce (the env
# override that pins the decide mode regardless of config/decide.yaml).
#
# Env knobs (all optional):
#   AB_LEDGER          ledger TSV path        (default tmp/ab_arms.tsv)
#   AB_MARKET_PROFILE  market profile id      (default ab_ai_infra_gtm)
#   SIGNALS_PY         python executable      (default ./.venv/Scripts/python.exe)
#
# Resume-aware: a domain whose arm+domain row already exists in the ledger
# is skipped. Per-domain error isolation: a failed domain is ledgered with
# its exit code and the loop continues. Per-domain stdout/stderr logs go to
# data/logs/ab/arm<A|B>_<domain>.log; the run log is data/logs/ab/run.log.
#
# No `--name` is passed on purpose: run_intel keeps the existing account's
# DB name (and derives one for new accounts), so the account_name term in
# the report's state-hash reconstruction stays stable across arms.
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)" || exit 1
cd "$ROOT" || exit 1

PY="${SIGNALS_PY:-./.venv/Scripts/python.exe}"
LEDGER="${AB_LEDGER:-tmp/ab_arms.tsv}"
PROFILE="${AB_MARKET_PROFILE:-ab_ai_infra_gtm}"
LOGDIR=data/logs/ab

usage() {
  cat >&2 <<'EOF'
usage: run_split_test.sh <arm> <domain> [more domains...]

  arm A  retrieval off  — set retrieval.enabled: false in config/default.yaml FIRST (manual pre-step; no env override exists)
  arm B  retrieval on   — set retrieval.enabled: true + build the index; runs --skip collect (frozen corpus)

env: AB_LEDGER (default tmp/ab_arms.tsv)  AB_MARKET_PROFILE (default ab_ai_infra_gtm)  SIGNALS_PY (default ./.venv/Scripts/python.exe)
EOF
}

if [ "$#" -lt 2 ]; then
  usage
  exit 2
fi

ARM="$1"; shift
case "$ARM" in
  A|B) ;;
  *) usage; echo "run_split_test.sh: arm must be A or B (got '$ARM')" >&2; exit 2 ;;
esac

mkdir -p "$LOGDIR" "$(dirname "$LEDGER")"

log() { echo "[$(date -u +%H:%M:%S)] $*" | tee -a "$LOGDIR/run.log"; }

# Informational posture reminder only — the script does NOT depend on (nor
# enforce) the current config value; the operator owns the flip (README.md).
posture="$(grep -A6 '^retrieval:' config/default.yaml 2>/dev/null | grep -E '^[[:space:]]*enabled:' | head -1 | tr -d '[:space:]')"
if [ "$ARM" = "A" ]; then
  log "ARM A start ($# domain(s)); ledger=$LEDGER"
  log "ARM A reminder: arm A requires retrieval.enabled: false — config/default.yaml now reads [${posture:-retrieval block not found}] (grep heuristic; verify by hand before trusting this arm)"
else
  log "ARM B start ($# domain(s)); ledger=$LEDGER"
  log "ARM B reminder: arm B requires retrieval.enabled: true AND a built index (check coverage: $PY -m src.cli index --stats) — config/default.yaml now reads [${posture:-retrieval block not found}] (grep heuristic; verify by hand)"
fi

for d in "$@"; do
  if awk -F'\t' -v arm="$ARM" -v dom="$d" '$1==arm && $2==dom{f=1} END{exit !f}' "$LEDGER" 2>/dev/null; then
    log "ARM $ARM $d already ledgered - skip"
    continue
  fi
  # Arm B freezes the corpus (--skip collect); arm A keeps the original
  # phase-2 form (collect runs, cadence-gated — run arm A right after the
  # corpus build so the cadence gates make it a no-op; see README.md).
  SKIP_ARGS=()
  if [ "$ARM" = "B" ]; then
    SKIP_ARGS=(--skip collect)
  fi
  log "ARM $ARM $d start"
  SIGNALS_DECIDE_MODE=enforce "$PY" -m src.cli intel "$d" \
    --market-profile "$PROFILE" --with-llm \
    ${SKIP_ARGS[@]+"${SKIP_ARGS[@]}"} \
    > "$LOGDIR/arm${ARM}_$d.log" 2>&1
  rc=$?
  pkg=$(ls -t data/dossiers 2>/dev/null | grep -E "^${d}-" | head -1)
  printf '%s\t%s\t%s\trc=%s\n' "$ARM" "$d" "$pkg" "$rc" >> "$LEDGER"
  log "ARM $ARM $d exit=$rc pkg=${pkg:-<none>}"
done
log "ARM $ARM ALL DONE"
