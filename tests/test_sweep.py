"""Sweep command: one-command account onboarding + full-source collection."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from src.core.models import Account


# ---------------------------------------------------------------------------
# run_sweep — orchestration logic (src/pipeline/sweep.py)
# ---------------------------------------------------------------------------


def _patch_registry(monkeypatch, accounts_by_domain):
    """Patch AccountRegistry so get() serves the given accounts and upsert
    records into the same dict (simulating persistence)."""
    from src.identity.registry import AccountRegistry

    upserts = []

    def get(self, domain):
        return accounts_by_domain.get(domain)

    def upsert(self, account, *, source=None):
        accounts_by_domain[account.domain] = account
        upserts.append(account)
        return account

    monkeypatch.setattr(AccountRegistry, "get", get)
    monkeypatch.setattr(AccountRegistry, "upsert", upsert)
    return upserts


def test_sweep_url_creates_account_and_collects(monkeypatch):
    from src.pipeline import sweep

    seen = {}

    def fake_collect(self, **kw):
        seen["collect"] = kw
        stats = {"fetched": 10, "signals_new": 2, "failed": 0}
        return stats

    monkeypatch.setattr(sweep, "orchestrator_collect", fake_collect)
    _patch_registry(monkeypatch, {})

    result = sweep.run_sweep("https://glow.security/about")

    assert result["domain"] == "glow.security"
    assert result["created"] is True
    assert result["collected"] == {"fetched": 10, "signals_new": 2, "failed": 0}
    # first-run account → force=True per scope decision (e)
    assert seen["collect"]["force"] is True
    assert seen["collect"]["domains"] == ["glow.security"]


def test_sweep_bare_domain_accepted(monkeypatch):
    from src.pipeline import sweep

    monkeypatch.setattr(sweep, "orchestrator_collect", lambda self, **kw: {"fetched": 0, "signals_new": 0, "failed": 0})
    _patch_registry(monkeypatch, {})

    result = sweep.run_sweep("wispr.ai")
    assert result["domain"] == "wispr.ai"
    assert result["created"] is True


def test_sweep_bare_company_name_refused(monkeypatch):
    from src.pipeline import sweep

    # no dot → company name → refused in v1 (no fuzzy search)
    with pytest.raises(ValueError, match="domain"):
        sweep.run_sweep("Glow")


def test_sweep_existing_account_respects_cadences(monkeypatch):
    from src.pipeline import sweep

    seen = {}

    def fake_collect(self, **kw):
        seen["collect"] = kw
        return {"fetched": 0, "signals_new": 0, "failed": 0}

    monkeypatch.setattr(sweep, "orchestrator_collect", fake_collect)
    existing = Account(domain="wispr.ai", name="Wispr")
    _patch_registry(monkeypatch, {"wispr.ai": existing})

    result = sweep.run_sweep("https://wispr.ai/blog")
    assert result["domain"] == "wispr.ai"
    assert result["created"] is False
    # existing account → cadences respected (no force)
    assert seen["collect"]["force"] is False


def test_sweep_explicit_force_override(monkeypatch):
    from src.pipeline import sweep

    seen = {}
    monkeypatch.setattr(
        sweep, "orchestrator_collect",
        lambda self, **kw: seen.update(collect=kw) or {"fetched": 0, "signals_new": 0, "failed": 0},
    )
    existing = Account(domain="wispr.ai", name="Wispr")
    _patch_registry(monkeypatch, {"wispr.ai": existing})

    sweep.run_sweep("wispr.ai", force_first_run=True)
    assert seen["collect"]["force"] is True


def test_sweep_reports_skipped_sources_with_reasons(monkeypatch):
    from src.pipeline import sweep

    monkeypatch.setattr(
        sweep, "orchestrator_collect",
        lambda self, **kw: {"fetched": 0, "signals_new": 0, "failed": 0},
    )
    _patch_registry(monkeypatch, {})

    class FakeAdapter:
        def __init__(self, key, requires):
            self.key = key
            self.requires = requires

    fake_adapters = [
        FakeAdapter("sec_edgar", ("cik",)),
        FakeAdapter("company_feed", ("blog_feed_url",)),
        FakeAdapter("techstack", ()),
    ]
    monkeypatch.setattr(sweep, "_enabled_adapters", lambda orch: fake_adapters)

    result = sweep.run_sweep("https://acme.io")
    skipped = dict(result["skipped"])
    assert skipped.get("sec_edgar") == "missing cik"
    assert skipped.get("company_feed") == "missing blog_feed_url"
    assert "techstack" not in skipped  # nothing required → ran, not skipped


def test_sweep_reminder_mentions_deepen(monkeypatch):
    from src.pipeline import sweep

    monkeypatch.setattr(
        sweep, "orchestrator_collect",
        lambda self, **kw: {"fetched": 0, "signals_new": 0, "failed": 0},
    )
    _patch_registry(monkeypatch, {})

    result = sweep.run_sweep("https://acme.io")
    assert any("deepen" in r for r in result["reminders"])


# ---------------------------------------------------------------------------
# CLI command wiring (src/cli.py sweep)
# ---------------------------------------------------------------------------


def _cli_setup(monkeypatch, accounts_by_domain=None):
    """Repo CLI-test pattern: patch Orchestrator.__init__ to a no-op and
    class-level attributes; never pass obj= to CliRunner.invoke."""
    from src.identity.registry import AccountRegistry
    from src.pipeline.orchestrator import Orchestrator

    accounts_by_domain = accounts_by_domain if accounts_by_domain is not None else {}
    monkeypatch.setattr(Orchestrator, "__init__", lambda self, *a, **k: None)
    registry = MagicMock()
    registry.get.side_effect = lambda d: accounts_by_domain.get(d)
    upserts = []

    def upsert(self, account, *, source=None):
        accounts_by_domain[account.domain] = account
        upserts.append(account)
        return account

    registry.upsert.side_effect = upsert
    monkeypatch.setattr(Orchestrator, "registry", registry, raising=False)
    return accounts_by_domain, upserts


def test_cli_sweep_runs_and_prints_digest(monkeypatch):
    from src.pipeline import sweep as sweep_mod
    from src.cli import main

    monkeypatch.setattr(
        sweep_mod, "orchestrator_collect",
        lambda orch, **kw: {"fetched": 5, "signals_new": 1, "failed": 0},
    )
    _cli_setup(monkeypatch)

    result = CliRunner().invoke(main, ["sweep", "https://glow.security/about"])
    assert result.exit_code == 0, result.output
    assert "glow.security" in result.output
    assert "signals_new=1" in result.output


def test_cli_sweep_refuses_bare_name(monkeypatch):
    from src.cli import main

    _cli_setup(monkeypatch)
    result = CliRunner().invoke(main, ["sweep", "Glow"])
    assert result.exit_code != 0
    assert "domain" in result.output.lower()
