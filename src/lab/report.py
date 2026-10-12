"""Calibration report + the propose-only decide.yaml diff (decide-lab Task 9).

:func:`build_report` renders the round loop's markdown artifact — per-variant
weighted scores WITH the split label that produced them, per-question
screening outcomes and importances, the baseline mandate line (the bar the
whole plan hangs on), the stability result, and the PROPOSAL section: a
fenced-yaml diff of proposed ``config/decide.yaml`` changes (threshold
ladders, question additions, band moves) derived from the round winners by
:func:`derive_proposal`.

The proposal is PROPOSE-ONLY, always: the lab never edits decide.yaml (the
plan's non-goal — application is a human step, the decide layer's enforce
rollout stays map-gated). :func:`write_proposal` writes the same structure
as a standalone yaml artifact.

DEFENSIVE BY CONTRACT: every section renders ``n/a`` on missing data — the
round dicts come from a loop whose steps each fail honestly (partial
budgets, skipped deciders, crashed rounds recorded as ``{"error": ...}``),
and a thin corpus must produce a thin report, never a crash and never a
fabricated number.

Input shapes (all produced by ``src/lab/__main__.py``; every field may be
absent):

* ``round_results`` — one dict per round: ``round``, ``target``, ``split``
  (the label for the variant scores), ``variants`` (replay_variant results:
  ``{variant_id, stage, status, score, calls, components}``),
  ``variant_specs`` (``{id: {stage, block, config_delta}}`` — the registry
  entries the round replayed), ``screening`` (prescreen rows), ``survivors``
  (passing candidates), ``importances`` (optional ``{question: value}``),
  ``models`` (fit_compare output or ``{"error": ...}``), ``stability``
  (stability_check output), ``errors`` (strings), ``budget``
  (``{used, total}``), and ``error`` (a round-level crash note).
* ``baselines_out`` — :func:`src.lab.baselines.score_baselines` output (or
  its honest ``{"error": ...}``).
* ``targets_stats`` — the targets module's stats dict
  (``{target, per_domain, overall}``).

Stdlib + PyYAML only (a core dependency), no pipeline imports (the scope
guard: src/lab never enters the intel import graph).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import yaml

from src.lab.replay import advance  # the >=epsilon advance rule, one source

#: The plan's headline invariant, verbatim from baselines.MANDATE context.
PROPOSAL_STATUS = "PROPOSAL — propose-only; application is a human step"

#: The advance margin a winner must clear over its incumbent (replay's plan
#: default — kept in one constant here so the report and the runner agree).
ADVANCE_EPSILON = 0.02

#: The proposed routing floor for a discovered question (variants.py's
#: PROFILE_VOCAB_MIN posture — additions ride the same ladder).
PROPOSED_QUESTION_FLOOR = 0.50

_NA = "n/a"
_PROPOSAL_NOTE = (
    "PROPOSAL ONLY — NOT APPLIED. The lab never edits config/decide.yaml; "
    "applying these changes is a human step (the decide layer's enforce "
    "rollout stays map-gated)."
)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _fmt(value, nd: int = 3) -> str:
    """One table cell: n/a for None, fixed float for numbers, str otherwise."""
    if value is None:
        return _NA
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.{nd}f}"
    return str(value)


def _short(text: str | None, limit: int = 60) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def question_slug(question: str) -> str:
    """A stable short id for a discovered question (normalized text hash)."""
    normalized = " ".join(str(question or "").casefold().split())
    return "q_" + hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:8]


# ------------------------------------------------------------------ sections


def _targets_section(targets_stats) -> list[str]:
    lines = ["## Target statistics", ""]
    stats = targets_stats if isinstance(targets_stats, dict) else None
    if not stats or not isinstance(stats.get("per_domain"), dict):
        lines += [f"target: {_fmt(stats.get('target')) if stats else _NA}", "", _NA, ""]
        return lines
    lines += [f"target: {_fmt(stats.get('target'))}", "",
              "| domain | n | positives | negatives | positive rate | skipped | sparse(<5%) |",
              "|---|---|---|---|---|---|---|"]
    per_domain: dict = stats["per_domain"]
    for domain in sorted(per_domain):
        ds = per_domain[domain] if isinstance(per_domain[domain], dict) else {}
        rate = ds.get("positive_rate")
        lines.append(
            f"| {domain} | {_fmt(ds.get('n'), 0)} | {_fmt(ds.get('positives'), 0)} "
            f"| {_fmt(ds.get('negatives'), 0)} | {_fmt(rate)} "
            f"| {_fmt(ds.get('skipped'), 0)} | {_fmt(ds.get('positives_below_5pct'))} |"
        )
    overall = stats.get("overall")
    if isinstance(overall, dict):
        lines.append(
            f"| **overall** | {_fmt(overall.get('n'), 0)} "
            f"| {_fmt(overall.get('positives'), 0)} | {_fmt(overall.get('negatives'), 0)} "
            f"| {_fmt(overall.get('positive_rate'))} | {_fmt(overall.get('skipped'), 0)} "
            f"| {_fmt(overall.get('positives_below_5pct'))} |"
        )
        reasons = overall.get("review_reasons") or {}
        if reasons:
            rendered = ", ".join(
                f"{name}={count}" for name, count in sorted(reasons.items(),
                                                             key=lambda kv: (-kv[1], kv[0]))
            )
            lines += ["", f"review reasons (overall): {rendered}"]
    lines.append("")
    return lines


def _baselines_section(baselines_out) -> list[str]:
    lines = ["## Baselines — the bar discovered questions must beat", ""]
    out = baselines_out if isinstance(baselines_out, dict) else {}
    if not out or out.get("error"):
        note = out.get("error") if out else "no baseline output"
        return lines + [f"{_NA} ({note})", "", f"mandate: {_NA}", ""]
    lines += ["| family | AUROC | PR-AUC | n | train/test | note |", "|---|---|---|---|---|---|"]
    for name, entry in out.items():
        if name in ("split", "mandate") or not isinstance(entry, dict):
            continue
        note = "unavailable" if entry.get("unavailable") else _fmt(entry.get("note"))
        lines.append(
            f"| {name} | {_fmt(entry.get('auroc'))} | {_fmt(entry.get('pr_auc'))} "
            f"| {_fmt(entry.get('n'), 0)} | {_fmt(entry.get('n_train'), 0)}/{_fmt(entry.get('n_test'), 0)} "
            f"| {note} |"
        )
    lines += ["", f"split: {_fmt(out.get('split'))}", "",
              f"mandate: {out.get('mandate') or _NA}", ""]
    return lines


def _variants_section(rd: dict) -> list[str]:
    lines = ["### Variant weighted scores", ""]
    variants = rd.get("variants")
    if not isinstance(variants, list) or not variants:
        return lines + [_NA, ""]
    split = _fmt(rd.get("split"))
    lines += [
        f"split: {split} (recorded outcomes are the ground truth)", "",
        "| variant | stage | status | weighted score | precision | recall | calls | split |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for res in variants:
        if not isinstance(res, dict):
            continue
        comp = res.get("components") if isinstance(res.get("components"), dict) else {}
        lines.append(
            f"| {res.get('variant_id') or _NA} | {_fmt(res.get('stage'))} "
            f"| {_fmt(res.get('status'))} | {_fmt(res.get('score'))} "
            f"| {_fmt(comp.get('acceptance_precision'))} | {_fmt(comp.get('claim_recall'))} "
            f"| {_fmt(res.get('calls'), 0)} | {split} |"
        )
    lines.append("")
    return lines


def _questions_section(rd: dict) -> list[str]:
    lines = ["### Questions (screening outcomes and importances)", ""]
    screening = rd.get("screening")
    importances = rd.get("importances") if isinstance(rd.get("importances"), dict) else {}
    if not isinstance(screening, list) or not screening:
        return lines + [_NA, ""]
    lines += [
        "| question | screening | failing/cause | screening score | importance | proposer |",
        "|---|---|---|---|---|---|",
    ]
    for row in screening:
        if not isinstance(row, dict):
            continue
        question = row.get("question")
        failing = ", ".join(row.get("failing") or []) or _fmt(row.get("cause"))
        passed = row.get("passed")
        outcome = row.get("outcome") or (passed if passed is not None else _NA)
        score = row.get("screening_score")
        if score is None and isinstance(row.get("nouls"), dict):
            values = [v for v in row["nouls"].values() if isinstance(v, (int, float))]
            score = sum(values) if values else None
        lines.append(
            f"| {_short(question)} | {_fmt(outcome)} | {failing} | {_fmt(score, 2)} "
            f"| {_fmt(importances.get(question))} | {_fmt(row.get('proposer'))} |"
        )
    lines.append("")
    return lines


def _models_section(rd: dict) -> list[str]:
    lines = ["### Supervised model comparison (fit_compare)", ""]
    out = rd.get("models")
    if not isinstance(out, dict) or not out or out.get("error"):
        error = out.get("error") if isinstance(out, dict) else None
        return lines + [f"{_NA}" + (f" ({error})" if error else ""), ""]

    def cell(entry: dict, metric: str) -> str:
        mean, std = entry.get(f"{metric}_mean"), entry.get(f"{metric}_std")
        if mean is None:
            return _NA
        return f"{mean:.3f}±{_fmt(std)}".replace("±n/a", "")

    lines += [
        f"split: {_fmt(out.get('split'))} | n: {_fmt(out.get('n'), 0)} "
        f"| features: {_fmt(out.get('n_features'), 0)} "
        f"| positive rate: {_fmt(out.get('positive_rate'))}", "",
        "| model | AUROC (mean±std) | PR-AUC (mean±std) | folds scored | note |",
        "|---|---|---|---|---|",
    ]
    for name, entry in (out.get("models") or {}).items():
        if not isinstance(entry, dict):
            continue
        if entry.get("skipped"):
            lines.append(f"| {name} | {_NA} | {_NA} | {_NA} | {entry.get('note') or 'skipped'} |")
            continue
        note = _NA
        for fold in entry.get("per_fold") or []:
            if isinstance(fold, dict) and fold.get("note"):
                note = fold["note"]
                break
        lines.append(
            f"| {name} | {cell(entry, 'auroc')} | {cell(entry, 'pr_auc')} "
            f"| {_fmt(entry.get('n_folds_scored'), 0)}/{_fmt(out.get('n_folds'), 0)} | {note} |"
        )
    lines.append("")
    return lines


def _stability_section(rd: dict) -> list[str]:
    lines = ["### Stability (sign holds across domain slices)", ""]
    stab = rd.get("stability")
    if not isinstance(stab, dict) or not stab:
        return lines + [_NA, ""]
    lines.append(
        f"slices: {_fmt(stab.get('n_slices'), 0)} "
        f"| keep threshold: {_fmt(stab.get('threshold'), 0)} "
        f"| stable: {len(stab.get('stable') or [])}"
    )
    stable = stab.get("stable") or []
    if stable:
        lines.append(f"stable: {', '.join(str(s) for s in stable)}")
    unstable = stab.get("unstable") or {}
    for name in sorted(unstable):
        counts = unstable[name] if isinstance(unstable[name], dict) else {}
        lines.append(
            f"unstable: {name} (+{counts.get('positive', 0)}/-{counts.get('negative', 0)}"
            f"/0:{counts.get('zero', 0)}/absent:{counts.get('absent', 0)})"
        )
    if not stable and not unstable:
        lines.append(_NA)
    lines.append("")
    return lines


def _errors_section(rd: dict) -> list[str]:
    lines = ["### Round errors / honesty", ""]
    errors = rd.get("errors")
    if rd.get("error"):
        lines.append(f"- ROUND FAILED (recorded, never raised): {rd['error']}")
    if isinstance(errors, list):
        for err in errors:
            lines.append(f"- {err}")
    if rd.get("stop"):
        lines.append(f"- STOP: {rd['stop']}")
    budget = rd.get("budget")
    if isinstance(budget, dict):
        lines.append(
            f"- budget: {budget.get('used', _NA)}/{budget.get('total', _NA)} calls used"
        )
    if len(lines) == 2:  # nothing appended: only the heading + blank
        lines.append(f"- {_NA}")
    lines.append("")
    return lines


def _round_section(rd: dict) -> list[str]:
    if not isinstance(rd, dict):
        return [f"## Round {_NA}", "", _NA, ""]
    lines = [f"## Round {rd.get('round', _NA)}", ""]
    if rd.get("target"):
        lines.append(f"target: {rd['target']}")
        lines.append("")
    lines += _variants_section(rd)
    lines += _questions_section(rd)
    lines += _models_section(rd)
    lines += _stability_section(rd)
    lines += _errors_section(rd)
    return lines


# ----------------------------------------------------------------- proposal


def derive_proposal(round_results) -> dict:
    """The propose-only decide.yaml diff, derived from the round winners.

    Winners: per stage, the best-scored variant that :func:`advance` clears
    over that stage's incumbent anchor (>= :data:`ADVANCE_EPSILON`). Each
    NUMERIC key in the winner's ``config_delta`` (threshold ladders, band
    moves, screen thresholds) becomes one change entry with from/to values
    (``from`` read off the incumbent's merged config — the shipped value).
    Screened survivors become ``question_additions`` (deduped by normalized
    question text across rounds). No winners and no survivors is an honest
    empty proposal, never an error.
    """
    changes: list[dict] = []
    additions: list[dict] = []
    seen_questions: set[str] = set()

    for rd in round_results or []:
        if not isinstance(rd, dict):
            continue
        results = [r for r in (rd.get("variants") or []) if isinstance(r, dict)]
        specs = rd.get("variant_specs") if isinstance(rd.get("variant_specs"), dict) else {}
        stages = {str(r.get("stage")) for r in results if r.get("stage")}
        for stage in sorted(stages):
            stage_results = [r for r in results if r.get("stage") == stage]
            incumbents = [
                r for r in stage_results
                if str(r.get("variant_id", "")).endswith("_incumbent")
                and r.get("score") is not None
            ]
            if not incumbents:
                continue
            incumbent = max(incumbents, key=lambda r: float(r["score"]))
            incumbent_cfg = incumbent.get("components") or {}
            incumbent_cfg = (
                incumbent_cfg.get("config") if isinstance(incumbent_cfg, dict) else {}
            ) or {}
            challengers = [
                r for r in stage_results
                if not str(r.get("variant_id", "")).endswith("_incumbent")
                and r.get("score") is not None
            ]
            if not challengers:
                continue
            winner = max(challengers, key=lambda r: float(r["score"]))
            if not advance(winner["score"], incumbent["score"], epsilon=ADVANCE_EPSILON):
                continue
            spec = specs.get(winner.get("variant_id"))
            spec = spec if isinstance(spec, dict) else {}
            winner_cfg = winner.get("components") or {}
            winner_cfg = (
                winner_cfg.get("config") if isinstance(winner_cfg, dict) else {}
            ) or {}
            for key, to in (spec.get("config_delta") or {}).items():
                if not isinstance(to, (int, float)) or isinstance(to, bool):
                    continue  # question maps etc. ride question_additions
                changes.append({
                    "block": spec.get("block"),
                    "key": key,
                    "from": incumbent_cfg.get(key),
                    "to": to,
                    "evidence": {
                        "round": rd.get("round"),
                        "variant": winner.get("variant_id"),
                        "score": winner.get("score"),
                        "incumbent_score": incumbent.get("score"),
                        "split": rd.get("split"),
                        "calls": winner.get("calls"),
                    },
                })
        for survivor in rd.get("survivors") or []:
            if not isinstance(survivor, dict):
                continue
            question = survivor.get("question") or survivor.get("text")
            if not isinstance(question, str) or not question.strip():
                continue
            key = " ".join(question.casefold().split())
            if key in seen_questions:
                continue
            seen_questions.add(key)
            criteria = survivor.get("criteria")
            instructions = question.rstrip("?.!") + "."
            if isinstance(criteria, str) and criteria.strip():
                instructions += f" Criteria: {criteria.strip()}."
            additions.append({
                "block": "document_gate",
                "question_id": question_slug(question),
                "instructions": instructions,
                "min_floor": PROPOSED_QUESTION_FLOOR,
                "evidence": {
                    "round": rd.get("round"),
                    "screening_score": survivor.get("screening_score"),
                    "proposer": survivor.get("proposer"),
                    "split": rd.get("split"),
                },
            })
    return {
        "status": PROPOSAL_STATUS,
        "changes": changes,
        "question_additions": additions,
    }


def _proposal_section(proposal: dict) -> list[str]:
    body = yaml.safe_dump(proposal, sort_keys=False, allow_unicode=True, width=88)
    return [
        "## PROPOSAL — proposed config/decide.yaml changes", "",
        f"> **{_PROPOSAL_NOTE}**", "",
        "```yaml",
        body.rstrip("\n"),
        "```", "",
    ]


# -------------------------------------------------------------------- the API


def build_report(
    round_results,
    baselines_out,
    targets_stats,
    *,
    out_path: str | Path | None = None,
) -> str:
    """Render the calibration report markdown; write it when ``out_path``
    is given (parents created). Returns the markdown text — identical to
    what was written. Defensive: every section renders n/a on missing data
    and no input shape raises."""
    rounds = [r for r in (round_results or [])]
    lines = [
        "# Decide-lab calibration report", "",
        f"generated: {_now()}",
        f"rounds: {len(rounds)}",
        "",
    ]
    lines += _targets_section(targets_stats)
    lines += _baselines_section(baselines_out)
    if rounds:
        for rd in rounds:
            lines += _round_section(rd)
    else:
        lines += ["## Rounds", "", _NA, ""]
    lines += _proposal_section(derive_proposal(rounds))
    lines.append(f"_application of the proposal is a human step — {PROPOSAL_STATUS.lower()}._")
    lines.append("")
    text = "\n".join(lines)
    if out_path is not None:
        path = Path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    return text


def write_proposal(proposal: dict, path: str | Path) -> Path:
    """Write the yaml-only proposal artifact (mkdir -p, LF-terminated).

    The proposal carries its own PROPOSAL status line, so the artifact is
    self-describing: it is a suggestion for a human, never an input the lab
    or the pipeline reads back."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(proposal if isinstance(proposal, dict) else {},
                          sort_keys=False, allow_unicode=True, width=88)
    out.write_text(body, encoding="utf-8", newline="\n")
    return out
