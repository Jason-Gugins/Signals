"""Wiring tests for the appstore/bbb/linkedin dispatch blocks in orchestrator.resolve
and the resolve CLI flags."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.core.models import Account


# ---------------------------------------------------------------- appstore ---
def test_resolve_appstore_flag_fills_app_store_id(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    from tests.test_orchestrator import _orch

    body = json.dumps(
        {"resultCount": 1, "results": [{"trackId": 1232780281, "trackName": "Acme"}]}
    ).encode()

    class Fetch:
        def get(self, task, **kw):
            return SimpleNamespace(ok=True, doc=SimpleNamespace(body=body))

    orch = _orch(tmp_path, fetcher=Fetch())
    orch.registry.upsert(Account(domain="acme.com", name="Acme"))
    out = orch.resolve(ats=False, cik=False, feeds=False, icp=False, appstore=True)
    assert out["appstore"] == 1
    assert orch.registry.get("acme.com").app_store_id == "1232780281"


def test_resolve_appstore_default_off(tmp_path):
    from tests.test_orchestrator import _orch

    class Boom:
        def get(self, task, **kw):
            raise AssertionError("appstore should be off by default")

    orch = _orch(tmp_path, fetcher=Boom())
    orch.registry.upsert(Account(domain="acme.com", name="Acme"))
    out = orch.resolve(ats=False, cik=False, feeds=False, icp=False)
    assert out["appstore"] == 0
    assert orch.registry.get("acme.com").app_store_id is None


# --------------------------------------------------------------------- bbb ---
def test_resolve_bbb_flag_fills_bbb_url(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    from tests.test_orchestrator import _orch

    body = json.dumps(
        {
            "totalResults": 1,
            "results": [
                {
                    "businessName": "Acme",
                    "reportUrl": "/us/ca/san-francisco/profile/acme/11-1",
                }
            ],
        }
    ).encode()

    class Fetch:
        def get(self, task, **kw):
            return SimpleNamespace(ok=True, doc=SimpleNamespace(body=body))

    orch = _orch(tmp_path, fetcher=Fetch())
    orch.registry.upsert(Account(domain="acme.com", name="Acme"))
    out = orch.resolve(ats=False, cik=False, feeds=False, icp=False, bbb=True)
    assert out["bbb"] == 1
    acct = orch.registry.get("acme.com")
    assert acct.extra_data.get("bbb_url") == (
        "https://www.bbb.org/us/ca/san-francisco/profile/acme/11-1"
    )


def test_resolve_bbb_default_off(tmp_path):
    from tests.test_orchestrator import _orch

    class Boom:
        def get(self, task, **kw):
            raise AssertionError("bbb should be off by default")

    orch = _orch(tmp_path, fetcher=Boom())
    orch.registry.upsert(Account(domain="acme.com", name="Acme"))
    out = orch.resolve(ats=False, cik=False, feeds=False, icp=False)
    assert out["bbb"] == 0


# ---------------------------------------------------------------- linkedin ---
def _fake_scraper_checkout(tmp_path: Path) -> Path:
    cwd = tmp_path / "Linkedin"
    venv = cwd / ".venv" / "Scripts"
    venv.mkdir(parents=True)
    (venv / "python.exe").write_text("", encoding="utf-8")
    return cwd


def test_resolve_linkedin_flag_fills_slug(tmp_path, monkeypatch):
    import sqlite3

    from tests.test_orchestrator import _orch

    cwd = _fake_scraper_checkout(tmp_path)
    db_path = cwd / "data" / "linkedin.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE companies (name TEXT PRIMARY KEY, linkedin_url TEXT,"
        " linkedin_slug TEXT, website TEXT, domain TEXT)"
    )
    conn.execute(
        "INSERT INTO companies (name, linkedin_slug, domain) VALUES ('acme', 'acme-co', 'acme.com')"
    )
    conn.commit()
    conn.close()

    class Proc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(argv, **kw):
        return Proc()

    monkeypatch.setattr(subprocess, "run", fake_run)
    orch = _orch(tmp_path)
    orch.config.external_dbs.linkedin_cli_cwd = str(cwd)
    orch.registry.upsert(Account(domain="acme.com", name="Acme"))
    out = orch.resolve(ats=False, cik=False, feeds=False, icp=False, linkedin=True)
    assert out["linkedin"] == 1
    assert orch.registry.get("acme.com").linkedin_slug == "acme-co"


def test_resolve_linkedin_default_off_no_subprocess(tmp_path, monkeypatch):
    from tests.test_orchestrator import _orch

    def boom_run(*a, **kw):
        raise AssertionError("linkedin should be off by default")

    monkeypatch.setattr(subprocess, "run", boom_run)
    orch = _orch(tmp_path)
    orch.registry.upsert(Account(domain="acme.com", name="Acme"))
    out = orch.resolve(ats=False, cik=False, feeds=False, icp=False)
    assert out["linkedin"] == 0


# -------------------------------------------------------------------- CLI ---
def test_cli_resolve_flags_forwarded():
    import inspect

    import click
    from src.cli import resolve

    params = {p.name: p for p in resolve.params}
    for flag in ("appstore", "bbb", "linkedin"):
        assert flag in params, f"missing --{flag} flag"
        assert params[flag].default is False


# --------------------------------------------------------------- discover ---
# Plan T4 default-off wiring contract (same Boom-fetcher pattern as above):
# the name->domain discovery waterfall is a SEPARATE opt-in path — it must
# never run inside resolve(), and the sweep verb must not discover unless
# --discover is passed.

def test_resolve_never_invokes_discovery_waterfall(tmp_path, monkeypatch):
    from src.identity import discover as discover_mod
    from tests.test_orchestrator import _orch

    def boom_waterfall(*a, **kw):
        raise AssertionError("discover_waterfall must not run inside resolve()")

    monkeypatch.setattr(discover_mod, "discover_waterfall", boom_waterfall)

    class Boom:
        def get(self, task, **kw):
            raise AssertionError("no network in default resolve()")

    orch = _orch(tmp_path, fetcher=Boom())
    orch.registry.upsert(Account(domain="acme.com", name="Acme"))
    out = orch.resolve(ats=False, cik=False, feeds=False, icp=False)
    assert out["accounts"] == 1


def test_cli_sweep_without_discover_flag_is_default_off(monkeypatch):
    from click.testing import CliRunner

    from src.cli import main
    from src.pipeline import sweep as sweep_mod
    from src.pipeline.orchestrator import Orchestrator

    def boom_discover(self, name, ddg=False):
        raise AssertionError("discovery is opt-in: must not run without --discover")

    monkeypatch.setattr(Orchestrator, "discover", boom_discover)
    monkeypatch.setattr(
        sweep_mod,
        "run_sweep",
        lambda url_or_name, **kw: {
            "domain": "x.io",
            "created": True,
            "collected": {"fetched": 0, "signals_new": 0, "failed": 0},
            "skipped": [],
            "reminders": [],
            "resolved": {},
        },
    )
    monkeypatch.setattr(Orchestrator, "__init__", lambda self, *a, **k: None)

    result = CliRunner().invoke(main, ["sweep", "https://x.io"])
    assert result.exit_code == 0, result.output
