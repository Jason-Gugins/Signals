"""Task 9 (decide-lab): the round runner + the calibration report.

Spec: docs/plans/2026-10-11-autoresearch-decide-lab.md — ``python -m src.lab
run`` executes propose -> prescreen -> replay -> encode -> regress -> errors
-> next round, bounded by --rounds/--budget-calls, never raising past a
round, and writes data/lab/report-<ts>.md with per-variant weighted scores
(split-labelled), per-question screening/importances, the baseline mandate,
stability, and a PROPOSAL-only fenced-yaml decide.yaml diff.

Fully OFFLINE end-to-end: the runner's documented seams are monkeypatched —
``src.lab.__main__._lab_decider`` (the lab decider factory) gets a scripted
Decider double, ``src.llm.implement.call_implementer`` (the proposers' own
documented seam) gets a fake, and ``src.lab.features.embedding_features``
is stubbed unavailable so no ONNX model is ever loaded. The synthetic
fixture world is the tests/test_lab_corpus.py pattern: tmp DB + raw store +
dossier packages whose document_gate rows join via state-hash and whose
citation rows carry persisted claims (the replayable history).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from src.core.db import Database
from src.core.rawstore import RawStore
from src.core.textutil import truncate
from src.decide import Decision
from src.decide.gates import state_hash
from src.lab import __main__ as lab_main
from src.lab import report as report_mod
from src.lab.report import build_report, derive_proposal, write_proposal

# --------------------------------------------------------------------- world


DOC1 = "Acme Ships Orbital Billing 2.0. " + ("lorem ipsum dolor sit amet " * 60)
DOC2 = "Acme raises Series F, confirms orbital billing GA."
DOC3 = "Beta Labs launches retry planner for flaky pipelines."

GOOD_QUESTION = "Does the document cite a concrete operational metric?"
BAD_QUESTION = "UNANSWERABLE: what is the document's karma?"


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _gate_row(doc_id: str, account: str, text: str, run_id: str, outcome: str,
              answers: dict) -> dict:
    return {
        "gate": "document_gate",
        "boundary": "document_gate",
        "state_hash": state_hash(
            {"doc_id": doc_id, "source": account, "text": truncate(text, 9600) or ""}
        ),
        "answers": answers,
        "deterministic_action": "include",
        "agree": None,
        "agree_direction": None,
        "error": None,
        "latency_ms": 100.0,
        "raw_tokens": 900,
        "model": "jev-1.13.0",
        "run_id": run_id,
        "called": True,
        "outcome": outcome,
        "reason": None,
    }


def _citation_row(run_id: str, batch: str, claim_idx: int, outcome: str,
                  answer: dict, claims: list[dict]) -> dict:
    return {
        "gate": "citation_soundness",
        "boundary": "citation_soundness",
        "state_hash": batch,
        "answers": {f"claim_{claim_idx}": answer},
        "deterministic_action": "accept",
        "agree": None,
        "agree_direction": None,
        "error": None,
        "latency_ms": 210.5,
        "raw_tokens": 1200,
        "model": "jev-1.13.0",
        "run_id": run_id,
        "called": True,
        "outcome": outcome,
        "noul": 0.9,
        "reason": "quote_anchored",
        "claims": claims,
    }


def _g4(label: str, prob: float) -> dict:
    return {"choice": label, "probabilities": {label: prob}}


def _doc_answers(relevant: float, evidence: float) -> dict:
    return {
        "is_relevant": {"noul": relevant},
        "contains_signal_evidence": {"noul": evidence},
        "contains_prompt_injection": {"noul": 0.05},
    }


def make_world(tmp_path: Path) -> dict:
    """Two domains, four packages, three joined docs, five labeled claims."""
    db = Database(tmp_path / "s.db")
    rs = RawStore(db, tmp_path / "raw")
    db.upsert("accounts", {"domain": "acme.com", "name": "Acme"}, pk="domain")
    db.upsert("accounts", {"domain": "beta.io", "name": "Beta Labs"}, pk="domain")

    d1 = rs.put(source="blog", url="https://acme.com/1", body=DOC1.encode(),
                content_type="text/html", status=200, domain="acme.com")
    d2 = rs.put(source="news_rss", url="https://acme.com/2", body=DOC2.encode(),
                content_type="text/html", status=200, domain="acme.com")
    d3 = rs.put(source="blog", url="https://beta.io/3", body=DOC3.encode(),
                content_type="text/html", status=200, domain="beta.io")

    claims_a = [
        {"text": "Acme shipped Orbital Billing 2.0.", "quote_span": "Acme Ships Orbital Billing 2.0.", "doc_id": d1.doc_id},
        {"text": "Acme raised a Series F.", "quote_span": "Acme raises Series F", "doc_id": d1.doc_id},
    ]
    claims_b = [{"text": "Beta launched a retry planner.", "quote_span": "Beta Labs launches", "doc_id": d3.doc_id}]

    dossiers = tmp_path / "dossiers"
    pkgs = {
        "acme.com-20261001T000000000000Z-run1": {
            "rows": [
                _gate_row(d1.doc_id, "Acme", DOC1, "run1", "included", _doc_answers(0.9, 0.8)),
                _gate_row(d2.doc_id, "Acme", DOC2, "run1", "excluded", _doc_answers(0.2, 0.8)),
                _citation_row("run1", "batch-a", 0, "accepted", _g4("supports", 0.92), claims_a),
                _citation_row("run1", "batch-a", 1, "reviewed", _g4("says_nothing", 0.95), claims_a),
            ],
            "manifest": {"domain": "acme.com", "market_profile_id": None,
                         "generated_at": "2026-10-01T00:00:02+00:00"},
        },
        "acme.com-20261002T000000000000Z-run2": {
            "rows": [
                _gate_row(d1.doc_id, "Acme", DOC1, "run2", "included", _doc_answers(0.9, 0.8)),
                _citation_row("run2", "batch-b", 0, "dropped", _g4("contradicted", 0.9), claims_a),
            ],
            "manifest": {"domain": "acme.com", "market_profile_id": None,
                         "generated_at": "2026-10-02T00:00:02+00:00"},
        },
        "beta.io-20261001T000000000000Z-run3": {
            "rows": [
                _gate_row(d3.doc_id, "Beta Labs", DOC3, "run3", "included", _doc_answers(0.9, 0.7)),
                _citation_row("run3", "batch-c", 0, "accepted", _g4("supports", 0.9), claims_b),
            ],
            "manifest": {"domain": "beta.io", "market_profile_id": None,
                         "generated_at": "2026-10-01T00:00:02+00:00"},
        },
        "beta.io-20261002T000000000000Z-run4": {
            "rows": [
                _gate_row(d3.doc_id, "Beta Labs", DOC3, "run4", "included", _doc_answers(0.9, 0.7)),
                _citation_row("run4", "batch-d", 0, "reviewed", _g4("says_nothing", 0.9), claims_b),
            ],
            "manifest": {"domain": "beta.io", "market_profile_id": None,
                         "generated_at": "2026-10-02T00:00:02+00:00"},
        },
    }
    for name, spec in pkgs.items():
        _write_jsonl(dossiers / name / "decisions.jsonl", spec["rows"])
        (dossiers / name / "manifest.json").write_text(
            json.dumps(spec["manifest"]), encoding="utf-8"
        )
    return {
        "db_path": tmp_path / "s.db",
        "raw_dir": tmp_path / "raw",
        "dossiers": dossiers,
        "out_dir": tmp_path / "lab-out",
    }


def run_args(world: dict, *extra: str) -> list[str]:
    return [
        "run",
        "--dossiers", str(world["dossiers"]),
        "--db", str(world["db_path"]),
        "--raw-dir", str(world["raw_dir"]),
        "--out-dir", str(world["out_dir"]),
        *extra,
    ]


# --------------------------------------------------------------------- fakes


class FakeDecider:
    """Screens every question at 0.9 — except the answerable Noul collapses
    for candidates carrying the UNANSWERABLE marker (a failed_floor row that
    must feed the next round's brief). Doc nouls 0.9/0.8 -> included; G4
    choices -> supports at 0.92."""

    model = "jev-fake"

    def __init__(self) -> None:
        self.calls: list[str | None] = []

    def decide(self, state, questions, gate=None):
        self.calls.append(gate)
        instructions = " ".join(
            str((q or {}).get("instructions") or "") for q in (questions or {}).values()
        )
        # prescreen candidates carry their question in the STATE (the question
        # is itself the state); doc-gate replays carry it in the instructions.
        state_question = str((state or {}).get("question")) if isinstance(state, dict) else ""
        marker = "UNANSWERABLE" in instructions or "UNANSWERABLE" in state_question
        answers = {}
        for qid, spec in (questions or {}).items():
            if (spec or {}).get("type") == "choice":
                answers[qid] = _g4("supports", 0.92)
                continue
            noul = 0.9
            if qid == "answerable_from_source" and marker:
                noul = 0.1
            answers[qid] = {"noul": noul}
        return Decision(applies=True, ok=True, answers=answers, raw_tokens=7)


class FakeImplementer:
    """The proposers' documented seam: two candidate questions per family."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, messages, *, model, base_url, api_key, **kwargs):
        self.calls.append({"messages": messages, "model": model})
        return {
            "questions": [
                {"text": GOOD_QUESTION, "criteria": "a number/date/name tied to operations"},
                {"text": BAD_QUESTION, "criteria": "outside any single document"},
            ]
        }


@pytest.fixture
def offline_seams(monkeypatch):
    """Wire the runner's three documented seams to offline fakes."""
    decider = FakeDecider()
    proposer = FakeImplementer()
    monkeypatch.setattr(lab_main, "_lab_decider", lambda cfg: decider)
    monkeypatch.setattr("src.llm.implement.call_implementer", proposer)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")  # the proposers' env gate
    from src.decide.jev import DATA_EXIT_CONSENT

    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", DATA_EXIT_CONSENT)  # egress consent (review fix)
    monkeypatch.setattr(
        "src.lab.features.embedding_features", lambda rows, **k: (None, "embedding")
    )
    return {"decider": decider, "proposer": proposer}


# ----------------------------------------------------------------- CLI shape


def test_parser_surface_names_subcommands_and_flags():
    parser = lab_main.build_parser()
    help_text = parser.format_help()
    for sub in ("run", "report", "corpus"):
        assert sub in help_text
    args = parser.parse_args([
        "run", "--rounds", "2", "--budget-calls", "25",
        "--target", "doc_useful", "--domains", "a.com", "b.io", "--dry-run",
    ])
    assert args.command == "run"
    assert (args.rounds, args.budget_calls, args.target) == (2, 25, "doc_useful")
    assert args.domains == ["a.com", "b.io"]
    assert args.dry_run is True
    rpt = parser.parse_args(["report", "rounds.jsonl"])
    assert rpt.command == "report" and rpt.rounds_jsonl == "rounds.jsonl"


def test_corpus_subcommand_prints_stats_without_writes(tmp_path, capsys):
    world = make_world(tmp_path)
    rc = lab_main.main([
        "corpus",
        "--dossiers", str(world["dossiers"]),
        "--db", str(world["db_path"]),
        "--raw-dir", str(world["raw_dir"]),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "10 rows" in out  # 4+2+2+2 decision rows
    assert "acme.com" in out and "beta.io" in out
    assert "claim_survival" in out and "doc_useful" in out
    assert "positive" in out.lower()
    # read-only: no lab output directory ever appears
    assert not (tmp_path / "lab-out").exists()


# -------------------------------------------------------------------- dry-run


def test_dry_run_prints_plan_and_writes_nothing(tmp_path, capsys, offline_seams):
    world = make_world(tmp_path)
    rc = lab_main.main(run_args(world, "--rounds", "1", "--budget-calls", "5", "--dry-run"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert "10 rows" in out            # the corpus size is in the plan
    assert "registry" in out.lower()   # the variant plan
    assert "budget" in out.lower()
    # NOTHING was written and NOTHING was called
    assert not world["out_dir"].exists()
    assert offline_seams["decider"].calls == []
    assert offline_seams["proposer"].calls == []


# ----------------------------------------------------------------- full runs


def _one_report(world) -> Path:
    reports = sorted(world["out_dir"].glob("report-*.md"))
    assert len(reports) == 1
    return reports[0]


def test_one_round_run_writes_report_rounds_and_proposal(tmp_path, offline_seams):
    world = make_world(tmp_path)
    rc = lab_main.main(run_args(world, "--rounds", "1", "--budget-calls", "200"))
    assert rc == 0
    report = _one_report(world)
    text = report.read_text(encoding="utf-8")
    assert "PROPOSAL" in text
    assert "doc_incumbent" in text and "g4_incumbent" in text
    assert GOOD_QUESTION[:30] in text          # the screened question surfaces
    assert "n/a" in text                       # honest thin-data slots
    # the companion artifacts: rounds jsonl + the yaml-only proposal
    rounds = sorted(world["out_dir"].glob("rounds-*.jsonl"))
    assert len(rounds) == 1
    lines = [json.loads(l) for l in rounds[0].read_text(encoding="utf-8").splitlines() if l]
    assert lines[0]["kind"] == "meta"
    assert any(l["kind"] == "round" for l in lines[1:])
    proposals = sorted(world["out_dir"].glob("proposal-*.yaml"))
    assert len(proposals) == 1
    loaded = yaml.safe_load(proposals[0].read_text(encoding="utf-8"))
    assert "PROPOSAL" in loaded["status"]


def test_report_variant_rows_carry_split_labels_and_registry_ids(tmp_path, offline_seams):
    world = make_world(tmp_path)
    assert lab_main.main(run_args(world, "--rounds", "1", "--budget-calls", "200")) == 0
    text = _one_report(world).read_text(encoding="utf-8")
    # every scored variant names the split that produced it
    assert "ledger-replay" in text
    for vid in ("doc_relevant_min_loose", "g4_floor_tight", "impl_prompt_claim_cap_6"):
        assert vid in text
    # statuses are part of the table (implementer variants are metadata-only)
    assert "not_replayable" in text


def test_discovered_questions_become_replayed_variants(tmp_path, offline_seams):
    world = make_world(tmp_path)
    assert lab_main.main(run_args(world, "--rounds", "1", "--budget-calls", "200")) == 0
    rounds = sorted(world["out_dir"].glob("rounds-*.jsonl"))[0]
    round_line = next(
        json.loads(l) for l in rounds.read_text(encoding="utf-8").splitlines()
        if json.loads(l).get("kind") == "round"
    )
    ids = [v["variant_id"] for v in round_line["variants"]]
    discovered = [i for i in ids if i.startswith("discovered_")]
    assert discovered, "screened survivors must enter the replay set"
    # replay rows were written for the discovered variant under rounds/<ts>/
    round_dirs = list((world["out_dir"] / "rounds").rglob("round-1"))
    assert round_dirs and any(round_dirs[0].glob("discovered_*.jsonl"))
    # and the survivor rides the proposal as a question addition
    proposal = yaml.safe_load(
        sorted(world["out_dir"].glob("proposal-*.yaml"))[0].read_text(encoding="utf-8")
    )
    additions = proposal.get("question_additions") or []
    assert any(GOOD_QUESTION[:30] in str(a.get("instructions")) for a in additions)


def test_budget_zero_completes_with_honest_empties(tmp_path, capsys, offline_seams):
    world = make_world(tmp_path)
    rc = lab_main.main(run_args(world, "--rounds", "1", "--budget-calls", "0"))
    assert rc == 0
    text = _one_report(world).read_text(encoding="utf-8")
    assert "PROPOSAL" in text
    assert "partial" in text            # jev variants honestly ran out of budget
    assert "budget" in text.lower()
    assert offline_seams["decider"].calls == []   # zero decider calls went out
    assert offline_seams["proposer"].calls == []  # proposers bounded by the same budget


def test_small_budget_bounds_every_decider_call(tmp_path, offline_seams):
    world = make_world(tmp_path)
    rc = lab_main.main(run_args(world, "--rounds", "1", "--budget-calls", "10"))
    assert rc == 0
    # the TOTAL across proposers + prescreen + replay never exceeds the budget
    total = len(offline_seams["decider"].calls) + len(offline_seams["proposer"].calls)
    assert total <= 10
    text = _one_report(world).read_text(encoding="utf-8")
    assert "partial" in text  # exhaustion mid-round is an honest partial


def test_run_skips_network_steps_honestly_without_key_or_consent(
    tmp_path, capsys, monkeypatch
):
    """No _lab_decider patch: with TYPESAFE key / consent / OpenRouter key
    all absent the run must still complete, skipping every network step with
    honest 'skipped' statuses (the seam factory itself reads the env)."""
    for var in ("TYPESAFE_API_KEY", "SIGNALS_DECIDE_DATA_EXIT", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(
        "src.lab.features.embedding_features", lambda rows, **k: (None, "embedding")
    )
    world = make_world(tmp_path)
    rc = lab_main.main(run_args(world, "--rounds", "1", "--budget-calls", "50"))
    assert rc == 0
    text = _one_report(world).read_text(encoding="utf-8")
    assert "skipped" in text
    # incumbents are zero-call: they still score honestly
    assert "doc_incumbent" in text and "g4_incumbent" in text
    rounds = sorted(world["out_dir"].glob("rounds-*.jsonl"))[0]
    round_line = next(
        json.loads(l) for l in rounds.read_text(encoding="utf-8").splitlines()
        if json.loads(l).get("kind") == "round"
    )
    statuses = {v["variant_id"]: v["status"] for v in round_line["variants"]}
    assert statuses["doc_incumbent"] == "complete"
    assert statuses["doc_relevant_min_loose"] == "skipped"
    assert round_line["errors"]


def test_never_raises_past_a_round(tmp_path, offline_seams, monkeypatch):
    """A crashing pipeline step is recorded in the report, never raised."""
    world = make_world(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("boom past the replay")

    monkeypatch.setattr("src.lab.replay.replay_variant", boom)
    rc = lab_main.main(run_args(world, "--rounds", "2", "--budget-calls", "50"))
    assert rc == 0
    text = _one_report(world).read_text(encoding="utf-8")
    assert "boom past the replay" in text
    assert "Round 1" in text and "Round 2" in text  # both rounds recorded


def test_report_subcommand_rerenders_from_rounds_jsonl(tmp_path, offline_seams):
    world = make_world(tmp_path)
    assert lab_main.main(run_args(world, "--rounds", "1", "--budget-calls", "200")) == 0
    rounds = sorted(world["out_dir"].glob("rounds-*.jsonl"))[0]
    out = tmp_path / "re-rendered.md"
    rc = lab_main.main(["report", str(rounds), "--out", str(out)])
    assert rc == 0
    text = out.read_text(encoding="utf-8")
    assert "PROPOSAL" in text
    assert "doc_incumbent" in text


def test_errors_feed_the_next_round_brief(tmp_path, offline_seams):
    """Round 1's screening failure (the UNANSWERABLE candidate) rides round
    2's brief as an error profile — the autoresearch feedback step."""
    world = make_world(tmp_path)
    rc = lab_main.main(run_args(world, "--rounds", "2", "--budget-calls", "200"))
    assert rc == 0
    calls = offline_seams["proposer"].calls
    assert len(calls) >= 4  # >= 3 families in round 1, more in round 2
    round2_user = calls[3]["messages"][1]["content"]
    assert "UNANSWERABLE" in round2_user
    assert "error_profile" in round2_user


# ------------------------------------------------------- report.py unit spec


def test_build_report_all_na_on_empty_inputs(tmp_path):
    out = tmp_path / "r.md"
    md = build_report([], {}, None, out_path=out)
    assert out.exists()
    assert md == out.read_text(encoding="utf-8")
    assert md.count("n/a") >= 5          # every section degrades honestly
    assert "PROPOSAL" in md              # the proposal section always exists
    assert "n/a" in md.split("PROPOSAL")[1].lower() or "changes: []" in md


def test_build_report_targets_stats_section_flags_sparse_domains(tmp_path):
    stats = {
        "target": "claim_survival",
        "per_domain": {
            "acme.com": {"n": 10, "positives": 9, "negatives": 1, "skipped": 2,
                         "skipped_no_claims": 2, "skipped_outcome": 0,
                         "skipped_unjoined": 0, "positive_rate": 0.9,
                         "positives_below_5pct": False,
                         "review_reasons": {"says_nothing": 4}},
            "beta.io": {"n": 3, "positives": 0, "negatives": 3, "skipped": 0,
                        "skipped_no_claims": 0, "skipped_outcome": 0,
                        "skipped_unjoined": 0, "positive_rate": 0.0,
                        "positives_below_5pct": True, "review_reasons": {}},
        },
        "overall": {"n": 13, "positives": 9, "negatives": 4, "skipped": 2,
                    "skipped_no_claims": 2, "skipped_outcome": 0,
                    "skipped_unjoined": 0, "positive_rate": 9 / 13,
                    "positives_below_5pct": False,
                    "review_reasons": {"says_nothing": 4}},
    }
    md = build_report([], {}, stats, out_path=None)
    assert "claim_survival" in md
    assert "acme.com" in md and "beta.io" in md
    assert "says_nothing" in md
    assert "yes" in md and "no" in md  # the sparse(<5%) honesty column


def test_build_report_baseline_mandate_line(tmp_path):
    baselines = {
        "deterministic": {"auroc": 0.71, "pr_auc": 0.40, "n": 20, "n_train": 10, "n_test": 10},
        "embedding": {"auroc": None, "pr_auc": None, "n": 20, "n_train": 10, "n_test": 10, "unavailable": True},
        "combined": {"auroc": 0.74, "pr_auc": 0.44, "n": 20, "n_train": 10, "n_test": 10},
        "split": "chronological",
        "mandate": ("discovered questions must beat the best single family. "
                    "Best single family: deterministic (AUROC 0.710, PR-AUC 0.400, chronological split)."),
    }
    md = build_report([], baselines, None, out_path=None)
    assert "discovered questions must beat the best single family" in md
    assert "chronological" in md
    assert "deterministic" in md
    # an error battery renders its note, never a crash
    md2 = build_report([], {"error": "sklearn unavailable"}, None, out_path=None)
    assert "sklearn unavailable" in md2


_SHIPPED_CFGS = {
    "doc_gate": {"relevant_min": 0.45, "evidence_min": 0.55},
    "g4": {"floor": 0.70, "band_low": 0.30},
}


def _fabricated_round(winner: dict | None = None, survivors: list | None = None) -> dict:
    """A minimal honest round: both stage incumbents (zero-call anchors
    carrying the shipped configs) plus one optional challenger."""
    variants = [
        {
            "variant_id": f"{stage}_incumbent", "stage": stage, "status": "complete",
            "score": 0.40, "calls": 0,
            "components": {"config": dict(cfg)},
        }
        for stage, cfg in _SHIPPED_CFGS.items()
    ]
    specs = {
        f"{stage}_incumbent": {"stage": stage, "block": "document_gate" if stage == "doc_gate" else "citation_soundness", "config_delta": {}}
        for stage in _SHIPPED_CFGS
    }
    if winner is not None:
        stage = winner.get("stage", "doc_gate")
        variants.append({
            "variant_id": winner["id"], "stage": stage,
            "status": "complete", "score": winner["score"], "calls": 3,
            "components": {"config": winner.get("config", {})},
        })
        specs[winner["id"]] = {
            "stage": stage,
            "block": winner.get("block", "document_gate" if stage == "doc_gate" else "citation_soundness"),
            "config_delta": winner["config_delta"],
        }
    return {
        "round": 1,
        "target": "claim_survival",
        "split": "ledger-replay (recorded outcomes)",
        "variants": variants,
        "variant_specs": specs,
        "screening": [],
        "survivors": survivors or [],
        "models": {"error": "sklearn unavailable"},
        "stability": None,
        "errors": [],
        "budget": {"used": 3, "total": 50},
    }


def test_build_report_stability_section():
    stability = {
        "n_slices": 3, "threshold": 2,
        "stable": ["profile_tfidf_cosine", "claim_count"],
        "unstable": {"char_count": {"positive": 1, "negative": 1, "zero": 0, "absent": 1}},
    }
    round_ = _fabricated_round()
    round_["stability"] = stability
    md = build_report([round_], {}, None, out_path=None)
    assert "stability" in md.lower()
    assert "profile_tfidf_cosine" in md and "claim_count" in md
    assert "char_count" in md


def test_derive_proposal_emits_threshold_change_for_round_winner():
    winner = {
        "id": "doc_evidence_min_tight", "score": 0.60,
        "config": {"relevant_min": 0.45, "evidence_min": 0.65},
        "config_delta": {"evidence_min": 0.65},
    }
    proposal = derive_proposal([_fabricated_round(winner=winner)])
    assert proposal["changes"], "a >=epsilon winner must propose its threshold move"
    change = proposal["changes"][0]
    assert change["block"] == "document_gate"
    assert change["key"] == "evidence_min"
    assert change["from"] == 0.55 and change["to"] == 0.65
    assert change["evidence"]["variant"] == "doc_evidence_min_tight"
    assert "PROPOSAL" in proposal["status"]


def test_derive_proposal_band_move_from_config_delta():
    winner = {
        "id": "g4_band_low_wide", "stage": "g4", "score": 0.55,
        "config": {"floor": 0.70, "band_low": 0.40},
        "config_delta": {"band_low": 0.40}, "block": "citation_soundness",
    }
    proposal = derive_proposal([_fabricated_round(winner=winner)])
    keys = {(c["block"], c["key"]) for c in proposal["changes"]}
    assert ("citation_soundness", "band_low") in keys


def test_derive_proposal_no_winners_means_empty_changes():
    # a sub-epsilon winner never proposes; ties never advance
    winner = {"id": "doc_relevant_min_loose", "score": 0.41,
              "config": {"relevant_min": 0.35}, "config_delta": {"relevant_min": 0.35}}
    proposal = derive_proposal([_fabricated_round(winner=winner)])
    assert proposal["changes"] == []


def test_derive_proposal_question_additions_from_survivors():
    survivors = [{
        "question": "Does the document cite a concrete operational metric?",
        "criteria": "a number/date/name tied to operations",
        "nouls": {"answerable_from_source": 0.9, "one_meaning_under_criteria": 0.9,
                  "applies_to_most_rows": 0.9, "varies_across_rows": 0.9},
        "screening_score": 3.6, "proposer": "test/bulk-m", "proposer_role": "bulk",
    }]
    proposal = derive_proposal([_fabricated_round(survivors=survivors)])
    assert len(proposal["question_additions"]) == 1
    addition = proposal["question_additions"][0]
    assert addition["block"] == "document_gate"
    assert addition["question_id"].startswith("q_")
    assert addition["evidence"]["screening_score"] == 3.6
    # dedupe: the same question in a later round is not proposed twice
    twice = derive_proposal([_fabricated_round(survivors=survivors),
                             _fabricated_round(survivors=survivors)])
    assert len(twice["question_additions"]) == 1


def test_write_proposal_round_trips_yaml(tmp_path):
    proposal = derive_proposal([_fabricated_round(winner={
        "id": "doc_relevant_min_tight", "score": 0.9,
        "config": {"relevant_min": 0.55}, "config_delta": {"relevant_min": 0.55},
    })])
    path = write_proposal(proposal, tmp_path / "nested" / "proposal.yaml")
    assert path.exists()
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert loaded == proposal


# ------------------------------------------------------ the decider factory


def test_lab_decider_factory_returns_none_without_credentials(monkeypatch):
    for var in ("TYPESAFE_API_KEY", "SIGNALS_DECIDE_DATA_EXIT"):
        monkeypatch.delenv(var, raising=False)
    assert lab_main._lab_decider({"decider": {"model": "jev-x"}}) is None


def test_lab_decider_factory_constructs_a_live_decider_directly(monkeypatch):
    """Mode forced on ONLY for the lab: a LiveDecider built straight from the
    decider block — the shipped get_decider memo (and its mode: off posture)
    is never touched, and no call goes out at construction time."""
    from src.decide import LiveDecider
    from src.decide import jev as jev_mod

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", jev_mod.DATA_EXIT_CONSENT)
    decider = lab_main._lab_decider({
        "decider": {
            "model": "jev-test-1", "base_url": "https://decider.example.invalid/v1",
            "timeout_s": 2.5, "breaker": {"min_calls": 3, "threshold": 0.5},
        }
    })
    try:
        assert isinstance(decider, LiveDecider)
        assert decider.model == "jev-test-1"
        assert decider.base_url == "https://decider.example.invalid/v1"
        assert decider.timeout_s == 2.5
        assert decider.breaker_min_calls == 3
        assert decider.error_rate_breaker == 0.5
        # the global memoized decider was never consulted or installed —
        # snapshot-and-compare, NOT absolute None: earlier test files in the
        # lane legitimately memoize a decider via get_decider(), so asserting
        # None is order-dependent and fails the full suite (review finding).
        memo_before = jev_mod._decider_instance
        jev_mod._decider_instance = sentinel = object()
        try:
            lab_main._lab_decider({"decider": {"model": "jev-x", "base_url": "u"}})
            assert jev_mod._decider_instance is sentinel
        finally:
            jev_mod._decider_instance = memo_before
    finally:
        decider.close()


# ---------------------------------------------------- report section details


def test_corpus_subcommand_domain_filter(tmp_path, capsys):
    world = make_world(tmp_path)
    rc = lab_main.main([
        "corpus", "--domains", "acme.com",
        "--dossiers", str(world["dossiers"]),
        "--db", str(world["db_path"]),
        "--raw-dir", str(world["raw_dir"]),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "6 rows" in out  # the two acme packages only
    assert "acme.com" in out
    assert "beta.io" not in out


def test_build_report_models_section_names_its_split_and_notes():
    models_out = {
        "split": "chronological", "n_folds": 2, "n": 10, "n_features": 4,
        "positive_rate": 0.4,
        "models": {
            "elastic_net_logistic": {
                "probabilistic": True,
                "auroc_mean": 0.61, "auroc_std": 0.02, "pr_auc_mean": 0.5,
                "pr_auc_std": 0.01, "n_folds_scored": 2,
                "per_fold": [{"note": "metrics undefined: single-class test split"}],
            },
            "catboost": {"skipped": True, "auroc_mean": None,
                         "note": "catboost not installed — optional extra"},
        },
    }
    round_ = _fabricated_round()
    round_["models"] = models_out
    md = build_report([round_], {}, None, out_path=None)
    assert "split: chronological" in md
    assert "0.610±0.020" in md
    assert "metrics undefined: single-class test split" in md
    assert "catboost not installed" in md
    assert "positive rate: 0.400" in md
