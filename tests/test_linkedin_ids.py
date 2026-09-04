"""Tests for LinkedIn slug resolution via the companion scraper subprocess."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from src.core.config import Config
from src.core.models import Account
from src.identity.linkedin_ids import LinkedinSlugResolver


class FakeRegistry:
    def __init__(self):
        self.upserts: list[tuple[Account, str | None]] = []

    def upsert(self, account: Account, *, source: str | None = None):
        self.upserts.append((account, source))
        return account


COMPANIES_SCHEMA = """
CREATE TABLE companies (
    name TEXT PRIMARY KEY,
    linkedin_url TEXT,
    linkedin_slug TEXT,
    website TEXT,
    domain TEXT
)
"""


@pytest.fixture
def scraper_env(tmp_path: Path):
    """Fake scraper checkout: venv exe + sqlite DB with a companies table."""
    cwd = tmp_path / "Linkedin"
    venv = cwd / ".venv" / "Scripts"
    venv.mkdir(parents=True)
    exe = venv / "python.exe"
    exe.write_text("", encoding="utf-8")
    db_path = cwd / "data" / "linkedin.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(COMPANIES_SCHEMA)
    conn.commit()
    conn.close()
    return cwd, exe, db_path


def _config_with_cwd(cwd: Path) -> Config:
    cfg = Config.load()
    cfg.external_dbs.linkedin_cli_cwd = str(cwd)
    return cfg


def _seed_db(db_path: Path, rows: list[tuple]) -> None:
    conn = sqlite3.connect(db_path)
    conn.executemany(
        "INSERT INTO companies (name, linkedin_url, linkedin_slug, website, domain) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def _make_run_recorder():
    calls: list[dict] = []

    def fake_run(argv, **kw):
        calls.append({"argv": argv, "kw": kw})
        # Write a matching row into the DB the way the real scraper would.
        seed_arg = argv[argv.index("--seed") + 1]
        payload = Path(seed_arg).read_text(encoding="utf-8").strip().splitlines()
        data_row = payload[1]  # row after header
        domain = data_row.split(",")[1].strip()
        conn = sqlite3.connect(str(Path(str(kw["cwd"])) / "data" / "linkedin.db"))
        conn.execute(
            "INSERT INTO companies (name, linkedin_url, linkedin_slug, domain) VALUES (?, ?, ?, ?)",
            (domain, f"https://www.linkedin.com/company/{domain}-co/", f"{domain}-co", domain),
        )
        conn.commit()
        conn.close()

        class Proc:
            returncode = 0
            stdout = "ok"
            stderr = ""

        return Proc()

    return fake_run, calls


def test_one_subprocess_per_batch_seed_csv_no_headed(scraper_env, tmp_path, monkeypatch):
    cwd, exe, db_path = scraper_env
    cfg = _config_with_cwd(cwd)
    fake_run, calls = _make_run_recorder()
    monkeypatch.setattr(subprocess, "run", fake_run)
    registry = FakeRegistry()
    accts = [
        Account(domain="notion.so", name="Notion"),
        Account(domain="acme.example", name="Acme"),
    ]
    out = LinkedinSlugResolver(cfg, registry).resolve_all(accts)
    assert len(calls) == 1  # batched: ONE subprocess for two accounts
    argv = calls[0]["argv"]
    assert str(exe) in argv
    assert "--seed" in argv
    assert "--headed" not in argv
    assert "login" not in argv
    seed_path = Path(argv[argv.index("--seed") + 1])
    assert seed_path.exists()
    lines = seed_path.read_text(encoding="utf-8").strip().splitlines()
    assert lines[0].strip().lower() == "name,domain"
    assert len(lines) == 3  # header + two accounts
    assert {line.split(",")[1].strip() for line in lines[1:]} == {"notion.so", "acme.example"}


def test_matching_domain_row_fills_slug_and_upserts(scraper_env, monkeypatch):
    cwd, exe, db_path = scraper_env
    cfg = _config_with_cwd(cwd)
    fake_run, calls = _make_run_recorder()
    monkeypatch.setattr(subprocess, "run", fake_run)
    registry = FakeRegistry()
    acct = Account(domain="notion.so", name="Notion")
    out = LinkedinSlugResolver(cfg, registry).resolve_all([acct])
    assert out == {"notion.so": "notion.so-co"}
    assert acct.linkedin_slug == "notion.so-co"
    assert len(registry.upserts) == 1
    assert registry.upserts[0][1] == "linkedin_ids"


def test_no_matching_row_returns_none(scraper_env, monkeypatch):
    cwd, exe, db_path = scraper_env
    cfg = _config_with_cwd(cwd)

    def fake_run(argv, **kw):
        class Proc:
            returncode = 0
            stdout = "ok"
            stderr = ""

        return Proc()

    monkeypatch.setattr(subprocess, "run", fake_run)
    registry = FakeRegistry()
    acct = Account(domain="missing.example", name="Missing")
    out = LinkedinSlugResolver(cfg, registry).resolve_all([acct])
    assert out == {"missing.example": None}
    assert acct.linkedin_slug is None
    assert registry.upserts == []


def test_subprocess_failure_returns_none_no_raise(scraper_env, monkeypatch):
    cwd, exe, db_path = scraper_env
    cfg = _config_with_cwd(cwd)

    def fake_run(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 600)

    monkeypatch.setattr(subprocess, "run", fake_run)
    registry = FakeRegistry()
    acct = Account(domain="stuck.example", name="Stuck")
    out = LinkedinSlugResolver(cfg, registry).resolve_all([acct])
    assert out == {"stuck.example": None}
    assert registry.upserts == []


def test_nonzero_returncode_returns_none(scraper_env, monkeypatch):
    cwd, exe, db_path = scraper_env
    cfg = _config_with_cwd(cwd)

    def fake_run(argv, **kw):
        class Proc:
            returncode = 3
            stdout = ""
            stderr = "boom"

        return Proc()

    monkeypatch.setattr(subprocess, "run", fake_run)
    registry = FakeRegistry()
    acct = Account(domain="fail.example", name="Fail")
    out = LinkedinSlugResolver(cfg, registry).resolve_all([acct])
    assert out == {"fail.example": None}
    assert registry.upserts == []


def test_missing_checkout_returns_empty_with_warning(scraper_env):
    cwd, exe, db_path = scraper_env
    cfg = Config.load()
    cfg.external_dbs.linkedin_cli_cwd = str(cwd.parent / "Nope")
    registry = FakeRegistry()
    acct = Account(domain="x.example", name="X")
    out = LinkedinSlugResolver(cfg, registry).resolve_all([acct])
    assert out == {}


def test_accounts_with_existing_slug_not_in_seed_csv(scraper_env, monkeypatch):
    cwd, exe, db_path = scraper_env
    cfg = _config_with_cwd(cwd)
    fake_run, calls = _make_run_recorder()
    monkeypatch.setattr(subprocess, "run", fake_run)
    registry = FakeRegistry()
    have = Account(domain="have.example", name="Have", linkedin_slug="have-co")
    need = Account(domain="need.example", name="Need")
    out = LinkedinSlugResolver(cfg, registry).resolve_all([have, need])
    assert out == {"have.example": "have-co", "need.example": "need.example-co"}
    seed_path = Path(calls[0]["argv"][calls[0]["argv"].index("--seed") + 1])
    lines = seed_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2  # header + only the account missing a slug
    assert "have.example" not in seed_path.read_text(encoding="utf-8")
