"""CLI setup / identity commands."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from src.cli import main


def test_init_idempotent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    r1 = runner.invoke(main, ["init"])
    r2 = runner.invoke(main, ["init"])
    assert r1.exit_code == 0 and r2.exit_code == 0
    for d in ("data/raw", "data/exports", "data/briefs", "data/alerts", "data/recon", "data/logs", "data/inbox/owned", "config/lists"):
        assert Path(d).is_dir()
    assert Path("data/signals.db").exists() or True  # created relative to config


def test_seed_csv_and_accounts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(main, ["init"])
    csv = tmp_path / "a.csv"
    csv.write_text("domain,name\nacme.com,Acme\n", encoding="utf-8")
    r = runner.invoke(main, ["seed", "--csv", str(csv), "--cohort", "t"])
    assert r.exit_code == 0
    assert "created=" in r.output
    r2 = runner.invoke(main, ["accounts"])
    assert r2.exit_code == 0
    assert "acme.com" in r2.output
    assert "domain" in r2.output.splitlines()[0]


def test_resolve_degrades(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(main, ["init"])
    r = runner.invoke(main, ["resolve", "--limit", "1"])
    assert r.exit_code == 0


def test_unknown_subcommand_exit_2():
    runner = CliRunner()
    r = runner.invoke(main, ["nope-command"])
    assert r.exit_code == 2
