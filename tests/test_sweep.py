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


def _patch_orch_resolve(monkeypatch, result=None):
    """Class-level Orchestrator.resolve patch (same pattern as the __init__
    no-op patch): records kw calls, returns a resolver-shaped dict."""
    from src.pipeline.orchestrator import Orchestrator

    calls = []

    def fake_resolve(self, **kw):
        calls.append(kw)
        base = {"accounts": 1, "cik": 0, "ats": 0, "feeds": 0, "icp": 0, "g2": 0}
        if result is not None:
            base.update(result)
        return base

    monkeypatch.setattr(Orchestrator, "resolve", fake_resolve)
    return calls


def test_sweep_url_creates_account_and_collects(monkeypatch):
    from src.pipeline import sweep

    seen = {}

    def fake_collect(self, **kw):
        seen["collect"] = kw
        stats = {"fetched": 10, "signals_new": 2, "failed": 0}
        return stats

    monkeypatch.setattr(sweep, "orchestrator_collect", fake_collect)
    _patch_registry(monkeypatch, {})
    _patch_orch_resolve(monkeypatch)

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
    _patch_orch_resolve(monkeypatch)

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
    _patch_orch_resolve(monkeypatch)

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
    _patch_orch_resolve(monkeypatch)

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
    # Default no-op resolver pass so sweep never reaches the network in CLI tests.
    monkeypatch.setattr(
        Orchestrator, "resolve",
        lambda self, **kw: {"accounts": 1, "cik": 0, "ats": 0, "feeds": 0, "icp": 0, "g2": 0},
    )
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


# ---------------------------------------------------------------------------
# Resolver pass on sweep (FEATURE 1) + --deep flag (FEATURE 2)
# ---------------------------------------------------------------------------


def test_sweep_new_account_runs_resolvers(monkeypatch):
    from src.pipeline import sweep

    monkeypatch.setattr(
        sweep, "orchestrator_collect",
        lambda self, **kw: {"fetched": 3, "signals_new": 1, "failed": 0},
    )
    _patch_registry(monkeypatch, {})
    calls = _patch_orch_resolve(monkeypatch, result={"cik": 1, "ats": 0, "feeds": 1, "icp": 0})

    result = sweep.run_sweep("https://glow.security/about")

    # resolve was called, restricted to this account's domain
    assert len(calls) == 1
    assert calls[0].get("domains") == ["glow.security"]
    assert result["resolved"] == {"cik": 1, "ats": 0, "feeds": 1, "icp": 0}
    # collect still ran
    assert result["collected"]["fetched"] == 3


def test_sweep_existing_account_skips_resolvers(monkeypatch):
    from src.pipeline import sweep

    monkeypatch.setattr(
        sweep, "orchestrator_collect",
        lambda self, **kw: {"fetched": 0, "signals_new": 0, "failed": 0},
    )
    existing = Account(domain="wispr.ai", name="Wispr")
    _patch_registry(monkeypatch, {"wispr.ai": existing})
    calls = _patch_orch_resolve(monkeypatch)

    result = sweep.run_sweep("https://wispr.ai/blog")

    assert len(calls) == 0
    assert result.get("resolved") == {}


def test_sweep_resolver_failure_never_fails_sweep(monkeypatch):
    from src.pipeline import sweep
    from src.pipeline.orchestrator import Orchestrator

    monkeypatch.setattr(
        sweep, "orchestrator_collect",
        lambda self, **kw: {"fetched": 7, "signals_new": 2, "failed": 0},
    )
    _patch_registry(monkeypatch, {})
    monkeypatch.setattr(
        Orchestrator, "resolve",
        lambda self, **kw: (_ for _ in ()).throw(RuntimeError("edgar 503")),
    )

    result = sweep.run_sweep("https://acme.io")

    assert result["resolved"] == {}
    assert result["collected"]["fetched"] == 7
    assert result["collected"]["signals_new"] == 2


def test_sweep_skipped_reflects_post_resolve(monkeypatch):
    from src.pipeline import sweep
    from src.pipeline.orchestrator import Orchestrator

    monkeypatch.setattr(
        sweep, "orchestrator_collect",
        lambda self, **kw: {"fetched": 0, "signals_new": 0, "failed": 0},
    )
    accounts: dict = {}
    _patch_registry(monkeypatch, accounts)

    class FakeAdapter:
        def __init__(self, key, requires):
            self.key = key
            self.requires = requires

    monkeypatch.setattr(
        sweep, "_enabled_adapters",
        lambda orch: [FakeAdapter("sec_edgar", ("cik",)), FakeAdapter("techstack", ())],
    )

    def fake_resolve(self, **kw):
        # Simulate the resolver actually filling cik on the account.
        acct = self.registry.get(kw["domains"][0])
        acct.cik = "0000320193"
        self.registry.upsert(acct)
        return {"accounts": 1, "cik": 1, "ats": 0, "feeds": 0, "icp": 0, "g2": 0}

    monkeypatch.setattr(Orchestrator, "resolve", fake_resolve)

    result = sweep.run_sweep("https://acme.io")

    skipped = dict(result["skipped"])
    assert "sec_edgar" not in skipped
    assert "techstack" not in skipped


def test_sweep_deep_flag_resolves_existing(monkeypatch):
    from src.pipeline import sweep

    monkeypatch.setattr(
        sweep, "orchestrator_collect",
        lambda self, **kw: {"fetched": 0, "signals_new": 0, "failed": 0},
    )
    existing = Account(domain="wispr.ai", name="Wispr")
    _patch_registry(monkeypatch, {"wispr.ai": existing})
    calls = _patch_orch_resolve(monkeypatch, result={"cik": 0, "ats": 1, "feeds": 0, "icp": 1})

    result = sweep.run_sweep("wispr.ai", deep=True)

    assert len(calls) == 1
    assert calls[0].get("domains") == ["wispr.ai"]
    assert result["resolved"] == {"cik": 0, "ats": 1, "feeds": 0, "icp": 1}
    assert result["deep"] is True


def test_sweep_deep_false_no_deep_key(monkeypatch):
    from src.pipeline import sweep

    monkeypatch.setattr(
        sweep, "orchestrator_collect",
        lambda self, **kw: {"fetched": 0, "signals_new": 0, "failed": 0},
    )
    _patch_registry(monkeypatch, {})
    _patch_orch_resolve(monkeypatch)

    result = sweep.run_sweep("acme.io")
    assert "deep" not in result


def test_cli_sweep_deep_flag(monkeypatch):
    from src.pipeline import sweep as sweep_mod
    from src.cli import main

    seen = {}

    def fake_run_sweep(url_or_name, *, force_first_run=None, deep=False):
        seen["deep"] = deep
        seen["url"] = url_or_name
        return {
            "domain": "x.io",
            "created": True,
            "collected": {"fetched": 0, "signals_new": 0, "failed": 0},
            "skipped": [],
            "reminders": [],
            "resolved": {},
            "deep": deep,
        }

    monkeypatch.setattr(sweep_mod, "run_sweep", fake_run_sweep)
    _cli_setup(monkeypatch)

    result = CliRunner().invoke(main, ["sweep", "https://x.io", "--deep"])
    assert result.exit_code == 0, result.output
    assert seen["deep"] is True
    assert seen["url"] == "https://x.io"


def test_cli_sweep_no_deep_flag(monkeypatch):
    from src.pipeline import sweep as sweep_mod
    from src.cli import main

    seen = {}

    def fake_run_sweep(url_or_name, *, force_first_run=None, deep=False):
        seen["deep"] = deep
        return {
            "domain": "x.io",
            "created": True,
            "collected": {"fetched": 0, "signals_new": 0, "failed": 0},
            "skipped": [],
            "reminders": [],
            "resolved": {},
        }

    monkeypatch.setattr(sweep_mod, "run_sweep", fake_run_sweep)
    _cli_setup(monkeypatch)

    result = CliRunner().invoke(main, ["sweep", "https://x.io"])
    assert result.exit_code == 0, result.output
    assert seen["deep"] is False
