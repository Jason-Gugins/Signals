"""``python -m src.lab`` — the decide-lab round runner + CLI (Task 9).

Dev-only: this package is never imported by the intel pipeline
(tests/test_lab_isolation.py enforces the import graph).

Subcommands:

  corpus  extract the ledger corpus from the dossiers + raw store and print
          row counts / target stats to stdout. READ-ONLY: no writes ever.
  run     the autoresearch round loop — load corpus -> targets -> baselines,
          then per round: propose (propose_batch, budgeted) -> prescreen
          (prescreen + prune_correlated) -> replay (surviving discovered
          questions + the variant registry, budgeted) -> encode + regress
          (fit_compare over the best-scoring feature family, chronological
          folds) -> this round's errors feed the next round's brief. The
          plateau rule (src/lab/models.py) stops the loop; every
          network-touching step (proposers, prescreen, replay decider) is
          bounded by --budget-calls TOTAL (decider/implementer INVOCATIONS — wire requests can reach 2x under the shipped max_retries) — exhaustion ends the round
          honestly with partial statuses. Artifacts: report-<ts>.md,
          rounds-<ts>.jsonl, proposal-<ts>.yaml under --out-dir, plus
          per-round replay JSONL under rounds/<ts>/round-N/.
  report  re-render the calibration report from a saved rounds JSONL.

NEVER RAISES PAST A ROUND: a crashing step is recorded on that round's dict
and the loop continues; the report says so.

GOAL-MODE NOTE: ``run`` is agent-ownable. The contract is exactly the CLI:
an idle-time agent may run ``python -m src.lab run --rounds N
--budget-calls N --target <t> [--domains ...]`` and nothing else — the
metric (the weighted battery), the budget (the TOTAL call ceiling) and the
stop rule (--rounds + the plateau rule) are all pinned by the invocation;
the loop's only outputs are the report/rounds/proposal artifacts. The
proposal is PROPOSE-ONLY: application of decide.yaml changes is a human
step. --dry-run prints the plan and writes/calls nothing — the safe probe.

DECIDER POSTURE: the lab needs a live Jev decider for prescreen/replay. It
constructs a LiveDecider DIRECTLY from the decider block (never
src.decide.jev.get_decider — its memoized instance follows the shipped
config's ``mode: off``), so the lab's spend is forced on ONLY for its own
calls and the global config is never flipped. Missing TYPESAFE_API_KEY or
SIGNALS_DECIDE_DATA_EXIT consent means no decider: the network steps are
SKIPPED HONESTLY (status "skipped", zero calls, never an error).

TEST SEAMS (documented, monkeypatchable — see tests/test_lab_runner.py):
  src.lab.__main__._lab_decider           the decider factory (returns None
                                          honestly without key/consent)
  src.llm.implement.call_implementer      the proposers' documented seam
  src.lab.features.embedding_features     accessed via the module attribute
  src.lab.replay.replay_variant           ditto (module-attribute call)
Everything else is offline: corpus/targets/baselines/models/splits never
touch the network.

Module top stays stdlib-only; the lab modules (and their numpy/sklearn)
import lazily inside the command functions.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

DEFAULT_DOSSIERS_DIR = "data/dossiers"
DEFAULT_DB = "data/signals.db"
DEFAULT_RAW_DIR = "data/raw"
DEFAULT_OUT_DIR = "data/lab"

DEFAULT_TARGET = "claim_survival"
TARGETS: tuple[str, ...] = ("claim_survival", "doc_useful")

#: One-line target definitions for the proposer briefs.
TARGET_DEFINITIONS: dict[str, str] = {
    "claim_survival": (
        "claim_survival: 1 when a judged claim's recorded citation-gate "
        "outcome is accepted, else 0 (reviewed/dropped)"
    ),
    "doc_useful": (
        "doc_useful: 1 when a staged document yielded >=1 accepted claim, "
        "else 0 (the document gate's real job)"
    ),
}

#: The split label every replayed variant score carries (the recorded
#: outcomes are the ground truth — the honest-numbers invariant).
VARIANT_SPLIT_LABEL = "ledger-replay (recorded outcomes)"

#: The prescreener's stratified sample size (the plan's 30-doc sample).
PRESCREEN_SAMPLE = 30

#: Rounds before the plateau stop fires without improvement (models.py's
#: default patience; kept here so the stop note can name it).
PLATEAU_PATIENCE = 2

_SUBCOMMANDS: tuple[tuple[str, str], ...] = (
    ("run", "execute autoresearch rounds (propose -> prescreen -> replay -> regress)"),
    ("report", "write the calibration report + proposed decide.yaml diff"),
    ("corpus", "extract the ledger corpus from data/dossiers into flat rows"),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.lab",
        description=(
            "Decide lab: offline, budget-bounded A/B + autoresearch loop that "
            "tunes the decide layer's spend stages from the decision ledger. "
            "Dev-only; never imported by the intel pipeline."
        ),
    )
    sub = parser.add_subparsers(dest="command", metavar="{run,report,corpus}")
    for name, text in _SUBCOMMANDS:
        sp = sub.add_parser(name, help=text)
        if name == "run":
            sp.add_argument("--rounds", type=int, default=3,
                            help="round loop length (the plateau rule may stop earlier)")
            sp.add_argument("--budget-calls", type=int, default=100,
                            help="TOTAL call ceiling across proposers + prescreen + replay")
            sp.add_argument("--target", choices=list(TARGETS), default=DEFAULT_TARGET,
                            help="supervised target (see src/lab/targets.py)")
            sp.add_argument("--domains", nargs="*", default=None,
                            help="restrict the corpus to these domains (default: all)")
            sp.add_argument("--dry-run", action="store_true",
                            help="print the plan; write nothing, call nothing")
            sp.add_argument("--dossiers", default=DEFAULT_DOSSIERS_DIR)
            sp.add_argument("--db", default=DEFAULT_DB,
                            help="signals DB (opened READ-ONLY; never migrated)")
            sp.add_argument("--raw-dir", default=DEFAULT_RAW_DIR)
            sp.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
        elif name == "corpus":
            sp.add_argument("--domains", nargs="*", default=None)
            sp.add_argument("--dossiers", default=DEFAULT_DOSSIERS_DIR)
            sp.add_argument("--db", default=DEFAULT_DB,
                            help="signals DB (opened READ-ONLY; never migrated)")
            sp.add_argument("--raw-dir", default=DEFAULT_RAW_DIR)
        else:  # report
            sp.add_argument("rounds_jsonl",
                            help="a rounds-<ts>.jsonl written by a previous run")
            sp.add_argument("--out", default=None,
                            help="output path (default: beside the input file)")
    return parser


# ------------------------------------------------------------------ plumbing


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class _ReadOnlyDB:
    """Minimal read-only Database stand-in (one/query — everything corpus +
    RawStore reads need). The lab never migrates or writes the signals DB:
    Database() would, so the runner opens the file read-only — and if the
    read-only open is impossible (missing file, locked, odd URI chars) it
    FAILS honestly: the old plain-connect fallback could silently CREATE an
    empty writable DB file (review finding), the opposite of read-only."""

    def __init__(self, path: Path) -> None:
        if not path.exists():
            raise FileNotFoundError(f"signals DB not found (read-only open refused): {path}")
        # percent-encode the path for the URI (Windows drive colons etc.)
        from urllib.parse import quote

        uri = f"file:{quote(path.as_posix())}?mode=ro"
        self._conn: sqlite3.Connection = sqlite3.connect(uri, uri=True)
        self._conn.row_factory = sqlite3.Row

    def one(self, sql: str, params=()):
        row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def query(self, sql: str, params=()):
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass


def _load_decide_cfg() -> dict:
    """config/decide.yaml ({} when absent/broken — the layer-off posture)."""
    from src.core.config import Config

    try:
        return Config().load_yaml("decide") or {}
    except FileNotFoundError:
        return {}
    except Exception as exc:  # unreadable yaml: run with defaults, say so
        logger.warning("lab: decide config unreadable ({}); using defaults", exc)
        return {}


def _lab_decider(decide_cfg: dict):
    """(TEST SEAM) The lab's live decider, or None — skip honestly.

    A LiveDecider is constructed DIRECTLY from the decider block so the lab
    forces the mode on ONLY for its own calls (the global get_decider memo
    and the shipped ``mode: off`` posture are never touched). Missing
    TYPESAFE_API_KEY or SIGNALS_DECIDE_DATA_EXIT consent means None: zero
    network, never an error.
    """
    from src.decide import jev

    decider_cfg = (decide_cfg or {}).get("decider") or {}
    if not isinstance(decider_cfg, dict):
        decider_cfg = {}
    api_key = os.environ.get("TYPESAFE_API_KEY")
    consent = os.environ.get("SIGNALS_DECIDE_DATA_EXIT")
    if not api_key or consent != jev.DATA_EXIT_CONSENT:
        logger.info(
            "lab: no live decider (missing TYPESAFE_API_KEY or data-exit consent) "
            "-> prescreen/replay network steps will be skipped honestly"
        )
        return None
    breaker_cfg = decider_cfg.get("breaker") or {}
    try:
        threshold = float(breaker_cfg.get(
            "threshold", decider_cfg.get("error_rate_breaker", jev.BREAKER_THRESHOLD)
        ))
    except (TypeError, ValueError):
        threshold = jev.BREAKER_THRESHOLD
    try:
        min_calls = int(breaker_cfg.get("min_calls", jev.BREAKER_MIN_CALLS))
    except (TypeError, ValueError):
        min_calls = jev.BREAKER_MIN_CALLS
    excluded = breaker_cfg.get("exclude_gates") or []
    if isinstance(excluded, str):
        excluded = [excluded]
    return jev.LiveDecider(
        base_url=str(decider_cfg.get("base_url") or jev.DEFAULT_BASE_URL),
        model=str(decider_cfg.get("model") or jev.DEFAULT_MODEL),
        api_key=api_key,
        timeout_s=float(decider_cfg.get("timeout_s", 5.0)),
        per_question_s=float(decider_cfg.get("per_question_s", jev.DEFAULT_PER_QUESTION_S)),
        max_retries=int(decider_cfg.get("max_retries", 1)),
        error_rate_breaker=threshold,
        breaker_min_calls=min_calls,
        breaker_exclude_gates=[str(g) for g in excluded],
    )


class _CallBudget:
    """The run's TOTAL call ceiling across every network-touching step."""

    def __init__(self, total: int) -> None:
        self.total = max(0, int(total))
        self.used = 0

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.used)

    def open(self, n: int = 1) -> bool:
        return self.remaining >= n

    def charge(self, n: int) -> None:
        self.used += max(0, int(n))
        if self.used > self.total:  # belt: the ledger never overstates spend
            logger.warning("lab: call budget overshot ({} > {})", self.used, self.total)
            self.used = self.total


@contextlib.contextmanager
def _counting_proposer_calls(counter: dict):
    """Count every proposer family call by wrapping the implementer seam for
    the duration of one propose_batch (restored after — the seam the test
    fakes ride is the same attribute)."""
    from src.llm import implement

    original = implement.call_implementer

    def counted(*args, **kwargs):
        counter["n"] = counter.get("n", 0) + 1
        return original(*args, **kwargs)

    implement.call_implementer = counted
    try:
        yield
    finally:
        implement.call_implementer = original


def _extract_rows(args) -> list[dict]:
    from src.lab import corpus as corpus_mod

    db = _ReadOnlyDB(Path(args.db))
    try:
        return corpus_mod.extract(
            db, args.dossiers, domains=args.domains, raw_dir=args.raw_dir
        )
    finally:
        db.close()


def _question_text(cand) -> str:
    if isinstance(cand, str):
        return cand
    if isinstance(cand, dict):
        text = cand.get("text")
        if not isinstance(text, str) or not text.strip():
            text = cand.get("question")
        return text if isinstance(text, str) else ""
    return ""


def _short(text: str | None, limit: int = 60) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _sample_states(rows: list[dict], limit: int = PRESCREEN_SAMPLE) -> list[str]:
    """The prescreener's stratified sample: joined doc heads, round-robin
    across domains (deterministic: domains in name order), capped at
    ``limit``."""
    by_domain: dict[str, list[str]] = {}
    for row in rows:
        if row.get("gate") != "document_gate" or not row.get("doc_joined"):
            continue
        text = row.get("text")
        if isinstance(text, str) and text:
            by_domain.setdefault(str(row.get("domain") or "unknown"), []).append(text)
    queues = [by_domain[d] for d in sorted(by_domain)]
    out: list[str] = []
    while queues and len(out) < limit:
        for queue in list(queues):
            if queue:
                out.append(queue.pop(0))
            if not queue:
                queues.remove(queue)
            if len(out) >= limit:
                break
    return out


def _market_profile(rows: list[dict]):
    """The dominant market profile for the corpus (for the deterministic
    family's vocabulary), or None — never raises, vocab just goes empty."""
    pids = [r.get("market_profile_id") for r in rows if r.get("market_profile_id")]
    if not pids:
        return None
    pid = Counter(str(p) for p in pids).most_common(1)[0][0]
    try:
        from src.intel.market import load_market_profile

        return load_market_profile(pid)
    except Exception as exc:
        logger.debug("lab: market profile {} unavailable ({}); empty vocabulary", pid, exc)
        return None


def _shipped_questions() -> list[dict]:
    """Round 1's current-question brief: the shipped doc-gate Nouls."""
    from src.lab import replay as replay_mod

    questions = replay_mod.doc_gate_questions("the account")
    return [
        {"text": f"{qid} (noul): {spec['instructions']}"}
        for qid, spec in questions.items()
    ]


def _discovered_variant(survivor: dict, index: int, round_no: int) -> dict:
    """One screened-survivor question as a registry-shaped doc-gate variant:
    the question rides the gate's batched request as an added Noul with its
    own routing floor (the has_profile_vocab_evidence pattern)."""
    from src.lab.report import question_slug
    from src.lab.proposers import normalize_text

    question = str(survivor.get("question") or "").strip()
    slug = question_slug(question) or (
        "q_" + hashlib.sha1(normalize_text(question).encode("utf-8")).hexdigest()[:8]
    )
    instructions = question.rstrip("?.!") + ". Answer noul."
    criteria = survivor.get("criteria")
    if isinstance(criteria, str) and criteria.strip():
        instructions += f" Criteria: {criteria.strip()}."
    return {
        "id": f"discovered_r{round_no}_{index:02d}_{slug}",
        "stage": "doc_gate",
        "block": "document_gate",
        "description": (
            f"Discovered question (round {round_no}, screening score "
            f"{survivor.get('screening_score')}): {question[:140]}"
        ),
        "config_delta": {
            "questions": {slug: {"type": "noul", "instructions": instructions}},
            f"{slug}_min": 0.50,
        },
        "requires_replay": "jev",
        "expected_direction": "probe",
        "discovered": True,
    }


def _best_family(baselines_out: dict, families: dict[str]) -> str | None:
    """The best single feature family by baseline AUROC (the bar), falling
    back to deterministic/whatever exists."""
    best_name, best = None, None
    if isinstance(baselines_out, dict):
        for name, entry in baselines_out.items():
            if name in ("split", "mandate", "combined") or not isinstance(entry, dict):
                continue
            auroc = entry.get("auroc")
            if auroc is None:
                continue
            if best is None or auroc > best:
                best_name, best = name, auroc
    if best_name in families:
        return best_name
    return "deterministic" if "deterministic" in families else next(iter(families), None)


def _model_folds(labeled: list[dict]):
    """Chronological folds (the primary split), degrading to a single
    holdout on thin corpora; None when even that is impossible."""
    from src.lab import splits as splits_mod

    for n_splits in (3, 2):
        try:
            return splits_mod.chronological_folds(labeled, n_splits)
        except ValueError:
            continue
    try:
        return [splits_mod.final_holdout(labeled)]
    except ValueError:
        return None


def _round_score(rd: dict) -> float | None:
    """The plateau history entry: the round's best supervised mean-fold
    AUROC (None when the supervised layer produced no number — plateau
    treats that as non-improving)."""
    out = rd.get("models") if isinstance(rd, dict) else None
    models_map = out.get("models") if isinstance(out, dict) else None
    best: float | None = None
    if isinstance(models_map, dict):
        for entry in models_map.values():
            if isinstance(entry, dict) and entry.get("auroc_mean") is not None:
                try:
                    value = float(entry["auroc_mean"])
                except (TypeError, ValueError):
                    continue
                best = value if best is None else max(best, value)
    return best


def _stability(labeled: list[dict], y: list[int], X, names: list[str]):
    """Sign-stability of every feature column's label correlation across up
    to 3 domain slices (models.stability_check's keep rule). None when the
    slices cannot support it — an honest n/a, never a fabricated verdict."""
    try:
        import numpy as np
    except ImportError:
        return None
    from src.lab import models as models_mod

    if X is None or not names or len(labeled) < 4:
        return None
    by_domain: dict[str, list[int]] = {}
    for i, row in enumerate(labeled):
        by_domain.setdefault(str(row.get("domain") or "unknown"), []).append(i)
    slices = [
        d
        for d in sorted(by_domain, key=lambda d: (-len(by_domain[d]), d))[:3]
        if len(by_domain[d]) >= 2 and len({y[i] for i in by_domain[d]}) == 2
    ]
    if len(slices) < 2:
        return None
    runs: dict[str, dict] = {}
    for domain in slices:
        idx = by_domain[domain]
        labels = np.asarray([y[i] for i in idx], dtype=float)
        run: dict[str, float | None] = {}
        for j, name in enumerate(names):
            col = np.asarray(X)[idx, j].astype(float)
            if float(np.std(col)) <= 0.0 or float(np.std(labels)) <= 0.0:
                run[name] = None
                continue
            r = float(np.corrcoef(col, labels)[0, 1])
            run[name] = None if (r != r or abs(r) == float("inf")) else r
        runs[domain] = run
    try:
        return models_mod.stability_check(runs)
    except Exception as exc:
        logger.warning("lab: stability check failed: {}", exc)
        return None


# ------------------------------------------------------------------- rounds


def _one_round(
    index: int,
    *,
    rows: list[dict],
    labeled: list[dict],
    y: list[int],
    families: dict[str, object],
    family_names: dict[str, list[str]],
    best_family: str | None,
    target: str,
    budget: _CallBudget,
    decider,
    decide_cfg: dict,
    base_cfgs: dict,
    run_id: str,
    out_dir: Path,
    questions: list[dict],
) -> tuple[dict, list[dict]]:
    """One autoresearch round. Returns (round_dict, next_round_questions);
    raises only to the runner's per-round belt (recorded, never past)."""
    from src.decide import NullDecider
    from src.lab import models as models_mod
    from src.lab import prescreen as prescreen_mod
    from src.lab import proposers as proposers_mod
    from src.lab import replay as replay_mod
    from src.lab import variants as variants_mod
    from src.lab.report import question_slug  # noqa: F401  (one slug source)

    errors: list[str] = []
    rd: dict = {
        "round": index,
        "target": target,
        "split": VARIANT_SPLIT_LABEL,
        "variants": [],
        "variant_specs": {},
        "screening": [],
        "survivors": [],
        "importances": {},  # v1: per-question importances need per-row noul
        "models": {},       # columns the corpus does not carry -> honest n/a
        "stability": None,
        "errors": errors,
        "budget": {},
    }

    # 1) propose — every family call counted against the TOTAL budget.
    candidates: list = []
    if budget.open(1):
        counter: dict = {"n": 0}
        failure: dict = {}
        with _counting_proposer_calls(counter):
            candidates = proposers_mod.propose_batch(
                decide_cfg,
                {"target": TARGET_DEFINITIONS.get(target, target), "questions": questions},
                budget_usd=budget.remaining * proposers_mod.DEFAULT_CALL_COST_USD,
                failure=failure,
            )
        budget.charge(counter["n"])
        if not candidates:
            errors.append(f"propose: no candidates ({failure.get('reason', 'unknown reason')})")
    else:
        errors.append("propose: call budget exhausted — no proposer calls this round")

    # 2) prescreen (+ correlation prune) — one decider call per candidate.
    screening: list = []
    survivors: list = []
    if candidates:
        if decider is None:
            screening = [
                {
                    "question": _question_text(c),
                    "outcome": prescreen_mod.OUTCOME_SKIPPED,
                    "passed": False,
                    "cause": "no live decider (missing TYPESAFE_API_KEY or data-exit consent)",
                }
                for c in candidates
            ]
            errors.append(
                "prescreen: skipped — no live decider (missing TYPESAFE_API_KEY "
                "or SIGNALS_DECIDE_DATA_EXIT consent)"
            )
        elif budget.remaining <= 0:
            screening = [
                {
                    "question": _question_text(c),
                    "outcome": prescreen_mod.OUTCOME_SKIPPED,
                    "passed": False,
                    "cause": "call budget exhausted",
                }
                for c in candidates
            ]
            errors.append("prescreen: skipped — call budget exhausted")
        else:
            chunk = candidates[: budget.remaining]
            survivors, screening = prescreen_mod.prescreen(
                chunk, _sample_states(rows), decider, run_id=run_id
            )
            budget.charge(len(chunk))
            for row in screening:
                outcome = row.get("outcome")
                if outcome == prescreen_mod.OUTCOME_FAILED_DECISION:
                    errors.append(
                        f"prescreen: decision failed for '{_short(row.get('question'))}' "
                        f"({row.get('cause')})"
                    )
                elif outcome == prescreen_mod.OUTCOME_FAILED_FLOOR:
                    errors.append(
                        f"prescreen: screened out '{_short(row.get('question'))}' "
                        f"({', '.join(row.get("failing") or []) or 'floor'})"
                    )
        survivors = prescreen_mod.prune_correlated(
            [s for s in survivors if isinstance(s, dict)]
        )
    rd["screening"] = screening
    rd["survivors"] = survivors

    # 3) replay the registry + the discovered-question variants.
    discovered = [
        _discovered_variant(s, i, index) for i, s in enumerate(survivors)
    ]
    all_variants = variants_mod.default_variants() + discovered
    round_dir = Path(out_dir) / "rounds" / run_id / f"round-{index}"
    specs: dict = rd["variant_specs"]
    for variant in all_variants:
        specs[variant["id"]] = {
            "stage": variant.get("stage"),
            "block": variant.get("block"),
            "config_delta": variant.get("config_delta"),
            "discovered": bool(variant.get("discovered")),
        }
        engine = variant.get("requires_replay")
        if engine == "jev" and decider is None:
            rd["variants"].append({
                "variant_id": variant["id"],
                "stage": variant.get("stage"),
                "score": None,
                "components": {
                    "note": "skipped: no live decider (key/consent missing)",
                    "n_scored": 0, "n_pending": 0, "n_skipped": 0,
                },
                "calls": 0,
                "status": "skipped",
            })
            continue
        limit = budget.remaining if engine == "jev" else 0
        result = replay_mod.replay_variant(
            variant,
            rows,
            decider if decider is not None else NullDecider(),
            budget_calls=limit,
            out_dir=round_dir,
            base_cfgs=base_cfgs,
        )
        budget.charge(int(result.get("calls") or 0))
        rd["variants"].append({
            k: result.get(k)
            for k in ("variant_id", "stage", "score", "components", "calls", "status")
        })
    if decider is None and any(v["status"] == "skipped" for v in rd["variants"]):
        errors.append(
            "replay: jev variants skipped — no live decider (TYPESAFE_API_KEY / consent missing)"
        )
    if budget.remaining <= 0:
        errors.append(
            f"replay: call budget exhausted after {budget.used} calls — "
            "unfinished variants carry honest partial statuses"
        )

    # 4) encode + regress over the best feature family.
    models_out: dict = {"error": "no feature families for the supervised layer"}
    if best_family and best_family in families:
        folds = _model_folds(labeled)
        if folds is None:
            models_out = {"error": "too few labeled rows for any chronological fold"}
        else:
            groups = [str(r.get("domain") or "unknown") for r in labeled]
            try:
                models_out = models_mod.fit_compare(
                    families[best_family], y, groups, folds
                )
            except Exception as exc:  # fitCompare's honest-error contract + belt
                models_out = {"error": f"fit_compare crashed: {exc}"}
    rd["models"] = models_out
    if isinstance(models_out, dict) and models_out.get("error"):
        errors.append(f"models: {models_out['error']}")

    # 5) stability over the same encoding's columns.
    if best_family and best_family in families:
        rd["stability"] = _stability(
            labeled, y, families[best_family], family_names.get(best_family) or []
        )

    rd["budget"] = {
        "used": budget.used, "total": budget.total, "remaining": budget.remaining
    }

    # 6) this round's errors feed the next round's brief (the loop step).
    next_questions: list[dict] = list(questions)
    for s in survivors:
        if isinstance(s, dict):
            next_questions.append({
                "text": s.get("question"),
                "criteria": s.get("criteria"),
                "screening_score": s.get("screening_score"),
            })
    for row in screening:
        if row.get("outcome") in (
            prescreen_mod.OUTCOME_FAILED_FLOOR, prescreen_mod.OUTCOME_FAILED_DECISION
        ):
            next_questions.append({
                "text": row.get("question"),
                "error_profile": {
                    "outcome": row.get("outcome"),
                    "failing": row.get("failing") or [],
                    "cause": row.get("cause"),
                },
            })
    if isinstance(models_out, dict) and models_out.get("error"):
        next_questions.append({
            "text": "(supervised layer)",
            "error_profile": {"outcome": "unavailable", "cause": models_out["error"]},
        })
    return rd, next_questions


# ---------------------------------------------------------------- commands


def _print_dry_run(args, rows: list[dict], decide_cfg: dict) -> None:
    from src.decide.jev import DATA_EXIT_CONSENT
    from src.lab import targets as targets_mod
    from src.lab import variants as variants_mod
    from src.lab.proposers import proposer_families

    target_fn = (
        targets_mod.claim_survival if args.target == "claim_survival"
        else targets_mod.doc_useful
    )
    labeled, stats = target_fn(rows)
    overall = stats.get("overall") or {}
    variants = variants_mod.default_variants()
    counts = Counter(v.get("stage") for v in variants)
    families = proposer_families(decide_cfg)
    domains = sorted({str(r.get("domain")) for r in rows if r.get("domain")})

    print("src.lab run — DRY-RUN: nothing will be written, no calls will go out")
    print(
        f"target: {args.target} | rounds: {args.rounds} | "
        f"budget: {args.budget_calls} invocations TOTAL (proposers + prescreen + replay; wire requests <= 2x under max_retries)"
    )
    suffix = f" (filtered: {', '.join(args.domains)})" if args.domains else ""
    print(f"corpus: {len(rows)} rows, {len(domains)} domains{suffix}")
    gates = [r for r in rows if r.get("gate") == "document_gate"]
    cites = [r for r in rows if r.get("gate") == "citation_soundness"]
    joined = sum(1 for r in gates if r.get("doc_joined"))
    with_claims = sum(
        1 for r in cites if isinstance(r.get("claims"), list) and r["claims"]
    )
    print(
        f"document_gate rows: {len(gates)} ({joined} join to stored docs) | "
        f"citation rows: {len(cites)} ({with_claims} carry persisted claims)"
    )
    rate = overall.get("positive_rate")
    rate_s = f"{rate:.1%}" if isinstance(rate, float) else "n/a"
    print(
        f"{args.target} labels: {overall.get('n', 0)} labeled rows, "
        f"{overall.get('positives', 0)} positives ({rate_s} positive)"
    )
    print(
        f"variant plan: {len(variants)} registry variants "
        f"(doc_gate {counts.get('doc_gate', 0)}, g4 {counts.get('g4', 0)}, "
        f"implementer {counts.get('implementer', 0)}) + discovered variants "
        "from this round's screening survivors (decider-bounded)"
    )
    fam_text = ", ".join(f"{f['role']}={f['model']}" for f in families) or "(none configured)"
    print(f"proposer families: {fam_text}")
    live = bool(os.environ.get("TYPESAFE_API_KEY")) and (
        os.environ.get("SIGNALS_DECIDE_DATA_EXIT") == DATA_EXIT_CONSENT
    )
    print(
        "decider: live (constructed from the decider block; lab-only mode)"
        if live
        else "decider: NOT live — prescreen/replay jev variants would be skipped honestly"
    )
    print(
        f"outputs on a real run: {args.out_dir}/report-<ts>.md, "
        f"rounds-<ts>.jsonl, proposal-<ts>.yaml (PROPOSAL only — never applied)"
    )


def _cmd_corpus(args) -> int:
    from src.lab import targets as targets_mod

    rows = _extract_rows(args)
    domains = sorted({str(r.get("domain")) for r in rows if r.get("domain")})
    packages = {r.get("package") for r in rows if r.get("package")}
    print(f"corpus: {len(rows)} rows from {len(packages)} packages across {len(domains)} domains")
    gate_counts = Counter(
        str(r.get("gate")) for r in rows if r.get("row_type") == "decision"
    )
    print("by gate: " + (", ".join(f"{g}={n}" for g, n in sorted(gate_counts.items())) or "(none)"))
    print(f"evidence rows: {sum(1 for r in rows if r.get('row_type') == 'evidence')}")
    for name, target_fn in (
        ("claim_survival", targets_mod.claim_survival),
        ("doc_useful", targets_mod.doc_useful),
    ):
        _labeled, stats = target_fn(rows)
        overall = stats.get("overall") or {}
        rate = overall.get("positive_rate")
        rate_s = f"{rate:.1%}" if isinstance(rate, float) else "n/a"
        sparse = " [sparse: <5% positives]" if overall.get("positives_below_5pct") else ""
        print(
            f"{name}: {overall.get('n', 0)} labeled rows, "
            f"{overall.get('positives', 0)} positives ({rate_s} positive){sparse}"
        )
        for domain in sorted(stats.get("per_domain") or {}):
            ds = stats["per_domain"][domain]
            print(f"  {domain}: n={ds.get('n', 0)}, positives={ds.get('positives', 0)}, skipped={ds.get('skipped', 0)}")
    return 0


def _cmd_run(args) -> int:
    from src.lab import baselines as baselines_mod
    from src.lab import features as features_mod
    from src.lab import models as models_mod
    from src.lab import report as report_mod
    from src.lab import targets as targets_mod

    decide_cfg = _load_decide_cfg()
    rows = _extract_rows(args)
    if args.dry_run:
        _print_dry_run(args, rows, decide_cfg)
        return 0

    target_fn = (
        targets_mod.claim_survival if args.target == "claim_survival"
        else targets_mod.doc_useful
    )
    labeled, targets_stats = target_fn(rows)
    y = [int(r.get("label") or 0) for r in labeled]

    # Feature families over the labeled rows (the encode step's inputs).
    profile = _market_profile(rows)
    families: dict[str, object] = {}
    family_names: dict[str, list[str]] = {}
    try:
        X_det, det_names = features_mod.deterministic_features(labeled, profile)
        families["deterministic"] = X_det
        family_names["deterministic"] = list(det_names)
    except Exception as exc:
        logger.warning("lab: deterministic family unavailable: {}", exc)
    try:
        X_emb, _name = features_mod.embedding_features(
            labeled, cache_dir=Path(args.out_dir) / "embeddings"
        )
        if X_emb is not None:
            families["embedding"] = X_emb
            family_names["embedding"] = [_name]
    except Exception as exc:
        logger.warning("lab: embedding family unavailable: {}", exc)

    try:
        baselines_out = (
            baselines_mod.score_baselines(
                {name: (X, y) for name, X in families.items()}, labeled
            )
            if families
            else {"error": "no feature families (empty labels or encoder unavailable)"}
        )
    except Exception as exc:
        baselines_out = {"error": f"baselines crashed: {exc}"}
    best_family = _best_family(baselines_out, families)

    budget = _CallBudget(args.budget_calls)
    decider = _lab_decider(decide_cfg)
    base_cfgs = (decide_cfg.get("decider") or {}).get("gates") or {}
    run_id = _ts()
    rounds: list[dict] = []
    history: list[float | None] = []
    questions = _shipped_questions()

    for index in range(1, max(0, args.rounds) + 1):
        try:
            rd, questions = _one_round(
                index,
                rows=rows,
                labeled=labeled,
                y=y,
                families=families,
                family_names=family_names,
                best_family=best_family,
                target=args.target,
                budget=budget,
                decider=decider,
                decide_cfg=decide_cfg,
                base_cfgs=base_cfgs,
                run_id=run_id,
                out_dir=Path(args.out_dir),
                questions=questions,
            )
        except Exception as exc:  # NEVER raises past a round
            logger.error("lab: round {} crashed (recorded): {}: {}", index, type(exc).__name__, exc)
            rd = {
                "round": index,
                "target": args.target,
                "split": VARIANT_SPLIT_LABEL,
                "error": f"{type(exc).__name__}: {exc}",
            }
        rounds.append(rd)
        history.append(_round_score(rd))
        try:
            if len(history) >= 2 and models_mod.plateau(history):
                rounds[-1]["stop"] = (
                    "plateau — no supervised-AUROC improvement >= epsilon for "
                    f"{PLATEAU_PATIENCE} consecutive rounds (src/lab/models.py)"
                )
                break
        except ValueError:
            pass

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / f"report-{run_id}.md"
    report_mod.build_report(
        rounds, baselines_out, targets_stats, out_path=report_path
    )
    rounds_path = out_dir / f"rounds-{run_id}.jsonl"
    meta = {
        "kind": "meta",
        "run_id": run_id,
        "target": args.target,
        "budget_calls": args.budget_calls,
        "domains": args.domains,
        "corpus_rows": len(rows),
        "labeled_rows": len(labeled),
        "targets_stats": targets_stats,
        "baselines": baselines_out,
    }
    with rounds_path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(meta, default=str) + "\n")
        for rd in rounds:
            fh.write(json.dumps({**rd, "kind": "round"}, default=str) + "\n")
    proposal = report_mod.derive_proposal(rounds)
    proposal_path = report_mod.write_proposal(proposal, out_dir / f"proposal-{run_id}.yaml")

    print(
        f"lab run {run_id}: {len(rounds)} round(s), "
        f"{budget.used}/{args.budget_calls} calls used"
    )
    print(f"report: {report_path}")
    print(f"rounds: {rounds_path}")
    print(
        f"proposal: {proposal_path} (PROPOSAL ONLY — application is a human step)"
    )
    return 0


def _cmd_report(args) -> int:
    from src.lab import report as report_mod

    path = Path(args.rounds_jsonl)
    if not path.exists():
        print(f"src.lab report: rounds file not found: {path}", file=sys.stderr)
        return 2
    meta: dict = {}
    rounds: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("kind") == "meta":
            meta = obj
        elif obj.get("kind") == "round":
            rounds.append({k: v for k, v in obj.items() if k != "kind"})
    out = args.out or str(path.with_name(path.stem + "-report.md"))
    report_mod.build_report(
        rounds, meta.get("baselines"), meta.get("targets_stats"), out_path=out
    )
    print(f"report written: {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    handlers = {"run": _cmd_run, "report": _cmd_report, "corpus": _cmd_corpus}
    try:
        return handlers[args.command](args)
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        print(
            f"src.lab {args.command}: failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
