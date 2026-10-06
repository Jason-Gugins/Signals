"""Plan T4 — CLI wiring for `sweep --discover NAME` (+ `--ddg`).

Follows the repo CLI-test pattern (tests/test_cli_pipeline.py /
tests/test_sweep.py): class-level Orchestrator patches, never a seeded
ctx.obj. The discovery path bypasses parse_target and run_sweep entirely —
no account creation anywhere in it.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from src.decide import Decision, MockDecider


def _fake_result(
    name="Acme Corp",
    status="ambiguous",
    domain=None,
    candidates=None,
    agreement=None,
    queued=True,
) -> dict:
    result = {
        "name": name,
        "status": status,
        "domain": domain,
        "candidates": candidates
        if candidates is not None
        else [
            {"domain": "acme.com", "score": 4, "stage": "wikidata", "label": "Acme Corp",
             "url": "https://acme.com"},
            {"domain": "acme.io", "score": 1, "stage": "ddg", "title": "Acme — official site",
             "url": "https://acme.io"},
        ],
        "stages": {
            "wikidata": {"status": "ambiguous"},
            "wikipedia": {"status": "skipped"},
            "gkg": {"status": "unconfigured"},
        },
        "queued": queued,
    }
    if agreement:
        result["agreement"] = agreement
    return result


def _patch_discover(monkeypatch, results=None, error_for=None):
    """Class-level Orchestrator.discover patch (Boom-contract style): records
    calls; returns canned results by name (a dict or a callable returning one,
    else a shared default)."""
    from src.pipeline.orchestrator import Orchestrator

    calls = []

    def fake_discover(self, name, ddg=False):
        calls.append({"name": name, "ddg": ddg})
        if error_for and name in error_for:
            raise error_for[name]
        table = results() if callable(results) else results
        if isinstance(table, dict):
            return table[name]
        return _fake_result(name=name)

    monkeypatch.setattr(Orchestrator, "discover", fake_discover)
    return calls


def _setup(monkeypatch):
    from src.pipeline.orchestrator import Orchestrator

    monkeypatch.setattr(Orchestrator, "__init__", lambda self, *a, **k: None)


def _forbid_run_sweep(monkeypatch):
    from src.pipeline import sweep as sweep_mod

    def boom(*a, **kw):
        raise AssertionError("run_sweep must not run in --discover mode")

    monkeypatch.setattr(sweep_mod, "run_sweep", boom)


# ── happy paths ─────────────────────────────────────────────────────────────

def test_cli_sweep_discover_prints_ranked_candidates_and_promote_hint(monkeypatch):
    from src.cli import main

    calls = _patch_discover(monkeypatch)
    _forbid_run_sweep(monkeypatch)
    _setup(monkeypatch)

    result = CliRunner().invoke(main, ["sweep", "--discover", "Acme Corp"])

    assert result.exit_code == 0, result.output
    assert calls == [{"name": "Acme Corp", "ddg": False}]
    assert "Acme Corp: ambiguous (2 candidates)" in result.output
    # Ranked report line: score, stage, domain, label, url.
    assert "4\twikidata\tacme.com\tAcme Corp\thttps://acme.com" in result.output
    assert "1\tddg\tacme.io\tAcme — official site\thttps://acme.io" in result.output
    assert "queued to identity_candidates; promote with: sweep acme.com" in result.output


def test_cli_sweep_discover_resolved_agreement_report(monkeypatch):
    from src.cli import main

    monkeypatch.setattr(
        "src.pipeline.orchestrator.Orchestrator.discover",
        lambda self, name, ddg=False: _fake_result(
            name=name, status="resolved", domain="acme.com",
            agreement=["wikidata", "gkg"],
            candidates=[
                {"domain": "acme.com", "score": 2, "stage": "wikidata", "label": "Acme"},
            ],
        ),
    )
    _forbid_run_sweep(monkeypatch)
    _setup(monkeypatch)

    result = CliRunner().invoke(main, ["sweep", "--discover", "Acme Corp"])

    assert result.exit_code == 0, result.output
    assert "Acme Corp: resolved -> acme.com (agreement: wikidata + gkg)" in result.output
    assert "queued to identity_candidates; promote with: sweep acme.com" in result.output


def test_cli_sweep_discover_bare_name_bypasses_parse_target(monkeypatch):
    from src.cli import main

    calls = _patch_discover(monkeypatch)
    _forbid_run_sweep(monkeypatch)
    _setup(monkeypatch)

    # No dot in the name: the sweep refusal must not fire in --discover mode.
    result = CliRunner().invoke(main, ["sweep", "--discover", "Glow Security"])

    assert result.exit_code == 0, result.output
    assert calls[0]["name"] == "Glow Security"
    assert "refused" not in result.output


def test_cli_sweep_discover_accepts_multiple_names(monkeypatch):
    from src.cli import main

    calls = _patch_discover(
        monkeypatch,
        results=lambda: {
            "Acme": _fake_result(name="Acme", status="no_match", candidates=[]),
            "Globex": _fake_result(name="Globex", status="no_match", candidates=[]),
        },
    )
    _forbid_run_sweep(monkeypatch)
    _setup(monkeypatch)

    result = CliRunner().invoke(main, ["sweep", "--discover", "Acme", "--discover", "Globex"])

    assert result.exit_code == 0, result.output
    assert [c["name"] for c in calls] == ["Acme", "Globex"]
    assert "Acme: no_match (0 candidates)" in result.output
    assert "Globex: no_match (0 candidates)" in result.output


def test_cli_sweep_discover_ddg_flag_flows_through(monkeypatch):
    from src.cli import main

    calls = _patch_discover(monkeypatch)
    _forbid_run_sweep(monkeypatch)
    _setup(monkeypatch)

    result = CliRunner().invoke(main, ["sweep", "--discover", "Acme", "--ddg"])
    assert result.exit_code == 0, result.output
    assert calls[-1]["ddg"] is True

    CliRunner().invoke(main, ["sweep", "--discover", "Acme"])
    assert calls[-1]["ddg"] is False  # default off


def test_cli_sweep_discover_per_name_failure_isolated(monkeypatch):
    from src.cli import main

    calls = _patch_discover(
        monkeypatch,
        results=lambda: {
            "Bad": None,
            "Good": _fake_result(name="Good", status="no_match", candidates=[]),
        },
        error_for={"Bad": RuntimeError("boom")},
    )
    _forbid_run_sweep(monkeypatch)
    _setup(monkeypatch)

    result = CliRunner().invoke(main, ["sweep", "--discover", "Bad", "--discover", "Good"])

    assert result.exit_code == 0, result.output
    assert [c["name"] for c in calls] == ["Bad", "Good"]
    assert "Bad: discovery failed: boom" in result.output
    assert "Good: no_match (0 candidates)" in result.output


# ── refusals / guardrails ───────────────────────────────────────────────────

def test_cli_sweep_discover_never_creates_accounts(monkeypatch):
    from src.cli import main
    from src.identity.registry import AccountRegistry

    _patch_discover(monkeypatch)
    _setup(monkeypatch)
    upserts = []
    monkeypatch.setattr(
        AccountRegistry, "upsert", lambda self, account, **kw: upserts.append(account)
    )

    result = CliRunner().invoke(main, ["sweep", "--discover", "Acme Corp"])

    assert result.exit_code == 0, result.output
    assert upserts == []


def test_cli_sweep_discover_conflicts_with_positional_target(monkeypatch):
    from src.cli import main

    calls = _patch_discover(monkeypatch)
    _setup(monkeypatch)

    result = CliRunner().invoke(main, ["sweep", "acme.io", "--discover", "Acme"])

    assert result.exit_code != 0
    assert "not both" in result.output
    assert calls == []


def test_cli_sweep_still_requires_target_without_discover(monkeypatch):
    from src.cli import main

    calls = _patch_discover(monkeypatch)
    _setup(monkeypatch)

    result = CliRunner().invoke(main, ["sweep"])

    assert result.exit_code != 0
    assert "URL_OR_NAME" in result.output
    assert calls == []


# ── decide-layer wiring (waves-2/3 Task 7: entity alignment) ────────────────
#
# The REAL Orchestrator.discover / discover_competitors run against a temp
# db (the test_competitor_news.py _orch pattern) with the discovery pass
# monkeypatched; the decide seam is monkeypatched the test_intel_llm_layer.py
# way (cfg loader + get_decider + attach identity — attach_decider refuses a
# MockDecider BY DESIGN). Layer-off byte-identity: no jev_alignment keys.

import json

WATERFALL_CANDIDATES = [
    {"domain": "acme.com", "score": 4, "stage": "wikidata", "label": "Acme Corp",
     "url": "https://acme.com"},
    {"domain": "acme.io", "score": 1, "stage": "ddg", "title": "Acme — official site",
     "url": "https://acme.io"},
    {"domain": "rival.com", "score": 1, "stage": "gkg", "label": "Rival Robotics"},
]

NOULS = {"same_name": 0.9, "same_domain": 0.8, "same_location": 0.3,
         "same_industry": 0.6}


class _CountingAlignmentDecider(MockDecider):
    """MockDecider that counts decide() invocations (one per aligned pair)."""

    def __init__(self, score=1.4, nouls=None, ok=True):
        answers = {"link_state": {"score": score, "confidence": 0.9}}
        for field, value in (nouls or NOULS).items():
            answers[field] = {"noul": value}
        super().__init__(
            {"link_state": Decision(applies=True, ok=ok, answers=answers,
                                    raw_tokens=50)}
        )
        self.n_calls = 0

    def decide(self, state, questions):
        self.n_calls += 1
        return super().decide(state, questions)


def _alignment_decider(score=1.4, nouls=None, ok=True):
    """Decider answering the five-question alignment batch in one Decision
    (keyed on the first question id, the test_decide_gates way)."""
    return _CountingAlignmentDecider(score=score, nouls=nouls, ok=ok)


def _decide_cfg(mode="enforce", enabled=True, max_pairs=10) -> dict:
    return {
        "mode": mode,
        "decider": {
            "gates": {
                "entity_alignment": {
                    "enabled": enabled, "max_pairs": max_pairs, "on_error": "skip",
                }
            }
        },
    }


def _patch_decide_layer(monkeypatch, *, decide_cfg, decider):
    """The intel _patch_layer pattern, orchestrator edition: canned decide
    config, canned decider, attach identity (MockDecider is refused by the
    real scope guard BY DESIGN)."""
    import src.pipeline.orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "_decide_cfg_of", lambda config: dict(decide_cfg))
    monkeypatch.setattr(orch_mod, "get_decider", lambda cfg: decider)
    monkeypatch.setattr(orch_mod.decide_policy, "attach_decider", lambda handle: handle)
    return decider


def _patch_waterfall(monkeypatch, result):
    """discover_waterfall patch at its module (discover imports it at call
    time); returns the recorded call names."""
    import src.identity.discover as dw

    names = []

    def fake_waterfall(name, fetcher, **kw):
        names.append(name)
        return json.loads(json.dumps(result))  # deep copy

    monkeypatch.setattr(dw, "discover_waterfall", fake_waterfall)
    return names


def _orch(tmp_path: Path):
    from src.core.config import Config
    from src.pipeline.orchestrator import Orchestrator

    cfg = Config()
    cfg.contact_email = "ops@example.com"
    cfg.storage.db_path = str(tmp_path / "s.db")
    cfg.storage.raw_dir = str(tmp_path / "raw")
    cfg.storage.briefs_dir = str(tmp_path / "briefs")
    cfg.storage.export_dir = str(tmp_path / "exports")
    cfg.config_dir = "config"
    return Orchestrator(cfg, fetcher=object())


def _stored(orch) -> list[dict]:
    row = orch.db.one("SELECT * FROM identity_candidates")
    assert row is not None
    return json.loads(row["candidates_json"])


def test_discover_annotates_candidates_with_jev_alignment(tmp_path, monkeypatch):
    result = {
        "name": "Acme Corp", "status": "ambiguous", "domain": None,
        "candidates": WATERFALL_CANDIDATES, "stages": {},
    }
    _patch_waterfall(monkeypatch, result)
    decider = _patch_decide_layer(
        monkeypatch, decide_cfg=_decide_cfg(), decider=_alignment_decider()
    )
    orch = _orch(tmp_path)
    try:
        out = orch.discover("Acme Corp")

        # One request per candidate pair, no cap hit (3 candidates < 10).
        assert decider.n_calls == 3
        stored = _stored(orch)
        assert [c["domain"] for c in stored] == ["acme.com", "acme.io", "rival.com"]
        for cand in stored:
            assert cand["jev_alignment"]["outcome"] == "related"
            assert cand["jev_alignment"]["score"] == 1.4
            assert cand["jev_alignment"]["fields"]["same_name"] == 0.9
            assert cand["jev_alignment"]["fields"]["same_location"] == 0.3
        # The returned result is the same annotation (one list, one truth).
        assert out["candidates"][0]["jev_alignment"]["outcome"] == "related"
        # NOTHING about the human decision changed: still pending.
        row = orch.db.one("SELECT * FROM identity_candidates")
        assert row["status"] == "pending"
        assert row["chosen_domain"] is None
    finally:
        orch.db.close()


def test_discover_mode_off_is_byte_identical(tmp_path, monkeypatch):
    result = {
        "name": "Acme Corp", "status": "ambiguous", "domain": None,
        "candidates": WATERFALL_CANDIDATES, "stages": {},
    }
    _patch_waterfall(monkeypatch, result)
    decider = _patch_decide_layer(
        monkeypatch, decide_cfg=_decide_cfg(mode="off"), decider=_alignment_decider()
    )
    orch = _orch(tmp_path)
    try:
        out = orch.discover("Acme Corp")

        # No decide call, no jev_alignment keys anywhere: byte-identical.
        assert decider.n_calls == 0
        stored = _stored(orch)
        for cand in stored:
            assert "jev_alignment" not in cand
        assert all("jev_alignment" not in c for c in out["candidates"])
        assert json.dumps(stored, sort_keys=True) == json.dumps(
            WATERFALL_CANDIDATES, sort_keys=True
        )
    finally:
        orch.db.close()


def test_discover_gate_disabled_is_byte_identical(tmp_path, monkeypatch):
    result = {
        "name": "Acme Corp", "status": "ambiguous", "domain": None,
        "candidates": WATERFALL_CANDIDATES, "stages": {},
    }
    _patch_waterfall(monkeypatch, result)
    decider = _patch_decide_layer(
        monkeypatch,
        decide_cfg=_decide_cfg(enabled=False),
        decider=_alignment_decider(),
    )
    orch = _orch(tmp_path)
    try:
        orch.discover("Acme Corp")
        assert decider.n_calls == 0
        for cand in _stored(orch):
            assert "jev_alignment" not in cand
    finally:
        orch.db.close()


class _FakeCompetitorPass:
    """Stands in for CompetitorNewsPass: one paypal competitor row."""

    def __init__(self, fetcher, registry=None):
        self.fetcher = fetcher
        self.registry = registry

    def discover(self, name):
        return {
            "status": "resolved_candidates",
            "name": name,
            "competitors": [
                {"name": "paypal", "title": "Stripe vs PayPal",
                 "url": "https://bing.example/1", "source": "bing_news",
                 "score": 1},
            ],
            "errors": {},
        }


def test_discover_competitors_annotates_competitor_rows(tmp_path, monkeypatch):
    """Same shape for kind=competitor rows — the join key is producer-
    agnostic (no domain: the competitor's own name joins the pair)."""
    import src.identity.competitor_news as cn

    monkeypatch.setattr(cn, "CompetitorNewsPass", _FakeCompetitorPass)
    decider = _patch_decide_layer(
        monkeypatch, decide_cfg=_decide_cfg(), decider=_alignment_decider()
    )
    orch = _orch(tmp_path)
    try:
        out = orch.discover_competitors("Stripe, Inc.")

        assert out["queued"] is True
        assert decider.n_calls == 1
        stored = _stored(orch)
        assert stored[0]["name"] == "paypal"
        assert stored[0]["jev_alignment"]["outcome"] == "related"
        assert stored[0]["jev_alignment"]["score"] == 1.4
        row = orch.db.one("SELECT * FROM identity_candidates")
        assert row["kind"] == "competitor"
        assert row["status"] == "pending"
    finally:
        orch.db.close()


def test_discover_competitors_mode_off_is_byte_identical(tmp_path, monkeypatch):
    import src.identity.competitor_news as cn

    monkeypatch.setattr(cn, "CompetitorNewsPass", _FakeCompetitorPass)
    decider = _patch_decide_layer(
        monkeypatch, decide_cfg=_decide_cfg(mode="off"), decider=_alignment_decider()
    )
    orch = _orch(tmp_path)
    try:
        out = orch.discover_competitors("Stripe")
        assert decider.n_calls == 0
        for cand in out["competitors"]:
            assert "jev_alignment" not in cand
        for cand in _stored(orch):
            assert "jev_alignment" not in cand
    finally:
        orch.db.close()


def test_discover_apex_agreement_unaffected_by_alignment(tmp_path, monkeypatch):
    """The deterministic 2-source apex auto-accept ladder runs FIRST and is
    untouched: the waterfall's resolved outcome survives annotation, and the
    agreeing candidate is annotated too (evidence, never a decision)."""
    result = {
        "name": "Acme",
        "status": "resolved",
        "domain": "acme.com",
        "agreement": ["wikidata", "gkg"],
        "candidates": [
            {"domain": "acme.com", "score": 2, "stage": "wikidata", "label": "Acme"},
        ],
        "stages": {},
    }
    _patch_waterfall(monkeypatch, result)
    decider = _patch_decide_layer(
        monkeypatch, decide_cfg=_decide_cfg(), decider=_alignment_decider()
    )
    orch = _orch(tmp_path)
    try:
        out = orch.discover("Acme")

        # The ladder's verdict stands exactly as the waterfall produced it.
        assert out["status"] == "resolved"
        assert out["domain"] == "acme.com"
        assert out["agreement"] == ["wikidata", "gkg"]
        # The queued row is still a pending human decision, now annotated.
        assert decider.n_calls == 1
        row = orch.db.one("SELECT * FROM identity_candidates")
        assert row["status"] == "pending"
        stored = json.loads(row["candidates_json"])
        assert stored[0]["jev_alignment"]["outcome"] == "related"
    finally:
        orch.db.close()
