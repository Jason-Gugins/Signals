"""Finding 6: the global fanout sources are opt-in for `intel`.

One single-company ``intel`` run made 189 live ``sec_formd`` requests and
seeded 60 unrelated accounts because three adapters plan GLOBALLY, not per
account. Those three (``sec_formd``, ``federal_register``, ``warn_notices``)
must run only when ``--include-fanout`` is passed; every other caller's
default behaviour is unchanged.

The marker is the existing ``SourceAdapter.fanout`` class attribute the
runner already uses to mean "plan once globally, parse per account". These
tests pin the opt-in end to end: the attribute, the orchestrator filter, the
coordinator wiring, the dossier gap and the CLI flag.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner

import src.cli as cli_mod
import src.pipeline.intel as intel_mod
from src.core.config import Config
from src.core.models import Account
from src.pipeline import intel
from src.pipeline.orchestrator import Orchestrator
from src.pipeline.runner import RunnerStats
from src.sources.registry import SOURCES

DOMAIN = "acme.com"
ROOT = Path(__file__).resolve().parents[1]

FANOUT_KEYS = ("sec_formd", "federal_register", "warn_notices")
CONTROL_KEYS = ("google_news", "ats_workday")

#: The exact gap the default (fanout-skipping) run must carry.
SKIP_GAP = (
    "fanout sources skipped (sec_formd, federal_register, warn_notices) "
    "- pass --include-fanout"
)


# ---------------------------------------------------------------------------
# 1. the marker attribute
# ---------------------------------------------------------------------------


def test_exactly_the_three_global_sources_are_marked_fanout():
    """`fanout is True` on exactly those three configured adapters.

    Restricted to the adapters named in ``config/sources.yaml`` so a
    test-registered dummy (several suites register fanout stand-ins) can
    never make this vacuous or flaky.
    """
    cfg = Config()
    cfg.config_dir = str(ROOT / "config")
    configured = set((cfg.load_yaml("sources").get("sources") or {}))
    fanout_keys = sorted(
        key
        for key in SOURCES
        if key in configured and getattr(SOURCES[key], "fanout", False)
    )
    assert fanout_keys == sorted(FANOUT_KEYS)


def test_control_adapters_are_not_fanout():
    for key in CONTROL_KEYS:
        assert not getattr(SOURCES[key], "fanout", False), key


def test_federal_contracts_is_still_not_fanout():
    # Out of scope for Finding 6 and asserted by tests/test_usaspending.py too.
    assert not getattr(SOURCES["federal_contracts"], "fanout", False)


# ---------------------------------------------------------------------------
# 2. the orchestrator filter
# ---------------------------------------------------------------------------


def _orch_with(adapters, config=None):
    orch = Orchestrator.__new__(Orchestrator)
    orch._adapters = list(adapters)
    orch.config = config if config is not None else Config()
    return orch


def _all_adapters():
    return [
        SOURCES[key]() for key in (*FANOUT_KEYS, *CONTROL_KEYS)
    ]


def test_pick_adapters_default_keeps_fanout_sources():
    orch = _orch_with(_all_adapters())

    keys = [a.key for a in orch._pick_adapters(None)]

    assert sorted(keys) == sorted((*FANOUT_KEYS, *CONTROL_KEYS))


def test_pick_adapters_skip_fanout_drops_exactly_the_globals():
    orch = _orch_with(_all_adapters())

    keys = sorted(a.key for a in orch._pick_adapters(None, skip_fanout=True))

    assert keys == sorted(CONTROL_KEYS)


def test_pick_adapters_skip_fanout_respects_an_explicit_sources_list():
    # An explicit sources= list is the caller's intent and wins over skip.
    orch = _orch_with(_all_adapters())

    keys = sorted(
        a.key for a in orch._pick_adapters(["sec_formd"], skip_fanout=True)
    )

    assert keys == ["sec_formd"]


def test_collect_signature_defaults_to_current_behaviour():
    import inspect

    params = inspect.signature(Orchestrator.collect).parameters
    assert "skip_fanout" in params
    assert params["skip_fanout"].default is False


# ---------------------------------------------------------------------------
# 3. the coordinator wiring + gaps
# ---------------------------------------------------------------------------


class _Registry:
    def __init__(self, accounts=None):
        self.accounts = dict(accounts or {})
        self.upserts = []

    def get(self, domain):
        return self.accounts.get(domain)

    def upsert(self, account, *, source=None):
        self.accounts[account.domain] = account
        self.upserts.append(account)
        return account


class _AccountsDB:
    """Counts `accounts` rows; the fanout stand-in adds rows during collect."""

    def __init__(self, n=1):
        self.n = n

    def one(self, sql, params=()):
        if "count" in sql.casefold():
            return {"n": self.n}
        return None

    def query(self, sql, params=()):
        return []


class _FakeOrch:
    def __init__(self, *, accounts=None, db=None, seed_accounts=0):
        self.registry = _Registry(accounts)
        self.db = db if db is not None else _AccountsDB(1)
        self.config = Config()
        self.calls = []
        self.seed_accounts = seed_accounts

    def resolve(self, **kw):
        self.calls.append(("resolve", kw))
        return {
            "accounts": 1, "cik": 0, "ats": 0, "feeds": 0, "icp": 0,
            "g2": 0, "appstore": 0, "bbb": 0, "linkedin": 0,
        }

    def collect(self, **kw):
        self.calls.append(("collect", kw))
        # Model the measured behaviour: the global fanout seeds accounts
        # during the PRIMARY collect. It only runs when not skipped.
        if kw.get("sources") is None and not kw.get("skip_fanout", False):
            self.db.n += self.seed_accounts
        stats = RunnerStats()
        stats.tasks = 2
        stats.signals_new = 1
        stats.mark("dummy_source", DOMAIN, "ran_empty")
        return stats

    def _record(self, name, kw):
        self.calls.append((name, kw))

    def score(self, **kw):
        self.calls.append(("score", kw))
        domain = list(kw["domains"])[0]
        snap = SimpleNamespace(domain=domain, today=__import__("datetime").date(2026, 9, 15))
        if kw.get("return_snapshots"):
            return {"scored": 1, "snapshots": {domain: snap}}
        return {"scored": 1}

    def find_careers(self, domain):  # pragma: no cover - must never be called
        raise AssertionError("run_intel must not call find_careers")


def _patch_outputs(monkeypatch):
    rec = {}

    def fake_build_coverage(*, config, account, outcomes, opt_in_flags=None, requested=None):
        return []

    def fake_build_dossier(snapshot, **kw):
        rec["gaps"] = list(kw.get("gaps") or [])
        return {"domain": getattr(snapshot, "domain", None)}

    def fake_write(dossier, *, out_dir):
        return {"package_dir": "pkg"}

    monkeypatch.setattr(intel, "build_coverage", fake_build_coverage)
    monkeypatch.setattr(intel, "build_dossier", fake_build_dossier)
    monkeypatch.setattr(intel, "write_intel_package", fake_write)
    return rec


def _existing(**kw):
    return _FakeOrch(accounts={DOMAIN: Account(domain=DOMAIN, name="Acme")}, **kw)


def _collect_calls(orch, *, sources):
    return [kw for n, kw in orch.calls if n == "collect" and kw.get("sources") == sources]


def test_default_run_skips_fanout_on_the_primary_collect(monkeypatch):
    orch = _existing()
    _patch_outputs(monkeypatch)

    intel.run_intel(DOMAIN, config=orch.config, orch=orch)

    primary = _collect_calls(orch, sources=None)
    assert len(primary) == 1
    assert primary[0]["skip_fanout"] is True


def test_include_fanout_runs_the_globals(monkeypatch):
    orch = _existing()
    _patch_outputs(monkeypatch)

    intel.run_intel(DOMAIN, config=orch.config, orch=orch, include_fanout=True)

    primary = _collect_calls(orch, sources=None)
    assert primary[0]["skip_fanout"] is False


def test_the_local_derive_call_is_never_told_to_skip(monkeypatch):
    orch = _existing()
    _patch_outputs(monkeypatch)

    intel.run_intel(DOMAIN, config=orch.config, orch=orch)

    derive = _collect_calls(orch, sources=list(intel.LOCAL_DERIVED_SOURCES))
    assert len(derive) == 1
    assert "skip_fanout" not in derive[0], (
        "the second (local-tier) collect must stay untouched"
    )


def test_default_run_records_the_skip_gap_for_the_dossier(monkeypatch):
    orch = _existing()
    rec = _patch_outputs(monkeypatch)

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch)

    assert SKIP_GAP in result["gaps"]
    assert SKIP_GAP in rec["gaps"], "the dossier must carry the skip gap"


def test_include_fanout_records_the_accounts_created(monkeypatch):
    orch = _existing(seed_accounts=60)
    rec = _patch_outputs(monkeypatch)

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, include_fanout=True)

    matching = [g for g in result["gaps"] if "accounts created during collect" in g]
    assert matching, result["gaps"]
    assert "60" in matching[0]
    assert matching[0] in rec["gaps"]
    assert not any(g == SKIP_GAP for g in result["gaps"])


def test_include_fanout_reports_zero_created(monkeypatch):
    orch = _existing(seed_accounts=0)
    _patch_outputs(monkeypatch)

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, include_fanout=True)

    matching = [g for g in result["gaps"] if "accounts created during collect" in g]
    assert matching, result["gaps"]
    assert " 0" in matching[0].split("collect:")[-1] or "collect: 0" in matching[0]


# ---------------------------------------------------------------------------
# 4. the CLI
# ---------------------------------------------------------------------------


def test_cli_intel_exposes_include_fanout_and_defaults_off():
    names = [p.name for p in cli_mod.intel.params]
    assert "include_fanout" in names, names
    opt = next(p for p in cli_mod.intel.params if p.name == "include_fanout")
    assert opt.is_flag
    assert opt.default is False


def test_cli_forwards_include_fanout(monkeypatch):
    calls = []

    def fake_run_intel(*args, **kwargs):
        calls.append((args, kwargs))
        return {
            "domain": DOMAIN,
            "created": True,
            "stages": {name: {"status": "ran"} for name in intel_mod.STAGES},
            "errors": {},
            "coverage": [],
            "gaps": [],
            "dossier": None,
            "paths": {},
        }

    monkeypatch.setattr(cli_mod, "Orchestrator", lambda cfg: object())
    monkeypatch.setattr(intel_mod, "run_intel", fake_run_intel)

    result = CliRunner().invoke(
        cli_mod.main, ["intel", DOMAIN, "--include-fanout"]
    )

    assert result.exit_code == 0, result.output
    assert calls[0][1]["include_fanout"] is True

    default = CliRunner().invoke(cli_mod.main, ["intel", DOMAIN])
    assert default.exit_code == 0, default.output
    assert calls[1][1]["include_fanout"] is False
