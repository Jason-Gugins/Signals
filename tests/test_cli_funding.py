"""CLI funding group wiring."""

from __future__ import annotations

from click.testing import CliRunner

from src.cli import main
from src.pipeline.funding import FundingStats
from src.pipeline.orchestrator import Orchestrator


def test_funding_commands_call_orchestrator(monkeypatch):
    seen = {}

    def funding(self, mode, **kw):
        seen["mode"] = mode
        seen["kw"] = kw
        return FundingStats(fetched=1, kept=1, signals_new=1, csv_path="data/exports/funding.csv")

    monkeypatch.setattr(Orchestrator, "funding", funding)
    runner = CliRunner()
    rec = runner.invoke(main, ["funding", "recent", "--days", "7", "--limit", "5"])
    assert rec.exit_code == 0, rec.output
    assert seen["mode"] == "recent"
    assert seen["kw"]["days"] == 7
    assert seen["kw"]["limit"] == 5
    assert "csv=" in rec.output

    dry = runner.invoke(main, ["--dry-run", "funding", "search", "robotics"])
    assert dry.exit_code == 0, dry.output
    assert seen["mode"] == "search"
    assert seen["kw"]["q"] == "robotics"
    assert seen["kw"]["dry_run"] is True

    bad_dry = runner.invoke(main, ["funding", "recent", "--dry-run"])
    assert bad_dry.exit_code == 2

    miss = runner.invoke(main, ["funding", "search"])
    assert miss.exit_code == 2

    none = runner.invoke(main, ["funding", "company"])
    assert none.exit_code == 2

    co = runner.invoke(main, ["funding", "company", "--cik", "0001234567"])
    assert co.exit_code == 0
    assert seen["mode"] == "company"
    assert seen["kw"]["cik"] == "0001234567"
    assert seen["kw"]["include_amendments"] is True

    new_only = runner.invoke(main, ["funding", "company", "--name", "Acme", "--new-only"])
    assert new_only.exit_code == 0
    assert seen["kw"]["include_amendments"] is False
    assert seen["kw"]["q"] == "Acme"
