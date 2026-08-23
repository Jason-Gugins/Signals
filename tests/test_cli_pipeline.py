"""CLI pipeline command wiring."""

from __future__ import annotations

from click.testing import CliRunner

from src.cli import main
from src.pipeline.orchestrator import Orchestrator
from src.pipeline.runner import RunnerStats


def test_commands_call_orchestrator(monkeypatch):
    seen = {}

    def collect(self, **kw):
        seen["collect"] = kw
        return RunnerStats()

    def reparse(self, **kw):
        seen["reparse"] = kw
        return RunnerStats()

    def score(self, **kw):
        seen["score"] = kw
        return {"scored": 0}

    def brief(self, **kw):
        seen["brief"] = kw
        return []

    def export(self, **kw):
        seen["export"] = kw
        return []

    def run_all(self, **kw):
        seen["run"] = kw
        return {}

    monkeypatch.setattr(Orchestrator, "collect", collect)
    monkeypatch.setattr(Orchestrator, "reparse", reparse)
    monkeypatch.setattr(Orchestrator, "score", score)
    monkeypatch.setattr(Orchestrator, "brief", brief)
    monkeypatch.setattr(Orchestrator, "export", export)
    monkeypatch.setattr(Orchestrator, "run_all", run_all)

    runner = CliRunner()
    assert runner.invoke(main, ["--dry-run", "collect", "--source", "sec", "--force"]).exit_code == 0
    assert seen["collect"]["dry_run"] is True
    assert seen["collect"]["force"] is True
    assert runner.invoke(main, ["reparse", "--since", "2026-01-01"]).exit_code == 0
    assert seen["reparse"]["since"] == "2026-01-01"
    assert runner.invoke(main, ["score", "--domain", "acme.com"]).exit_code == 0
    assert runner.invoke(main, ["brief", "--tier-max", "1"]).exit_code == 0
    assert runner.invoke(main, ["export", "--format", "csv", "--what", "funding"]).exit_code == 0
    assert seen["export"]["what"] == "funding"
    assert runner.invoke(main, ["run", "--skip-collect"]).exit_code == 0
    assert seen["run"]["skip_collect"] is True


def test_run_strict_exit_codes(monkeypatch):
    def boom(self, **kw):
        if kw.get("strict"):
            raise RuntimeError("fail")
        return {"errors": {"collect": "fail"}}

    monkeypatch.setattr(Orchestrator, "run_all", boom)
    runner = CliRunner()
    ok = runner.invoke(main, ["run"])
    assert ok.exit_code == 0
    bad = runner.invoke(main, ["run", "--strict"])
    assert bad.exit_code == 1
