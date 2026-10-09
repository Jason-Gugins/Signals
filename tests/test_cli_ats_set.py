"""Tests for the `ats-set` CLI command — manual ATS vendor/token stamping.

ats-set is the human-gated escape hatch for ATS stamping (plan
2026-10-09_184424-pilot-defect-fixes.md Task 3): it validates VENDOR against
COLLECTED_VENDORS and stamps EXISTING accounts only — it must never create an
account row (the never-guess contract; accounts are created by sweep).

Follows the repo's CLI test pattern (tests/test_prune.py): monkeypatch
src.cli.Config.load so the command's direct Database(cfg.storage.db_path)
construction lands on a tmp DB.
"""
from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from src.cli import main
from src.core.config import Config
from src.core.db import Database
from src.core.models import Account
from src.identity.registry import AccountRegistry


def _db_path(tmp_path: Path) -> str:
    return str(tmp_path / "signals.db")


def _registry(tmp_path: Path) -> AccountRegistry:
    return AccountRegistry(Database(_db_path(tmp_path)))


def _seed(tmp_path: Path, domain: str, name: str = "Acme") -> None:
    _registry(tmp_path).upsert(Account(domain=domain, name=name), source="test")


def _stored(tmp_path: Path, domain: str):
    # Fresh connection: assert what the CLI actually committed.
    return AccountRegistry(Database(_db_path(tmp_path))).get(domain)


def _invoke(tmp_path: Path, monkeypatch, *args: str):
    cfg = Config()
    cfg.storage.db_path = _db_path(tmp_path)
    monkeypatch.setattr("src.cli.Config.load", lambda *a, **k: cfg)
    return CliRunner().invoke(main, ["ats-set", *args])


def test_happy_path_stamps_existing_account(tmp_path, monkeypatch):
    _seed(tmp_path, "acme.com")
    result = _invoke(tmp_path, monkeypatch, "acme.com", "greenhouse", "acme")
    assert result.exit_code == 0, result.output
    assert "stamped: acme.com ats_vendor=greenhouse ats_token=acme" in result.output
    acct = _stored(tmp_path, "acme.com")
    assert acct is not None
    assert acct.ats_vendor == "greenhouse"
    assert acct.ats_token == "acme"


def test_unknown_vendor_lists_valid_vendors(tmp_path, monkeypatch):
    _seed(tmp_path, "acme.com")
    result = _invoke(tmp_path, monkeypatch, "acme.com", "taleso", "tok")
    assert result.exit_code != 0, result.output
    assert "taleso" in result.output
    # the refusal names the authoritative vendor set (src/sources/registry.py)
    assert "greenhouse" in result.output


def test_unknown_account_refused_and_never_created(tmp_path, monkeypatch):
    result = _invoke(tmp_path, monkeypatch, "ghost.io", "greenhouse", "x")
    assert result.exit_code != 0, result.output
    assert "unknown account" in result.output
    # never-guess: a refused stamp must not have created the account row
    assert _stored(tmp_path, "ghost.io") is None


def test_workday_compound_token_stored_verbatim(tmp_path, monkeypatch):
    _seed(tmp_path, "acme.com")
    result = _invoke(
        tmp_path, monkeypatch, "acme.com", "Workday", "darktrace/wd3/Site"
    )
    assert result.exit_code == 0, result.output
    acct = _stored(tmp_path, "acme.com")
    assert acct.ats_vendor == "workday"  # casefolded on the way in
    assert acct.ats_token == "darktrace/wd3/Site"  # stored as-given, '/' intact
