"""Tests for the master intelligence flow coordinator (Task 12).

``run_intel`` wires one domain through every capability:

    identity -> collect -> derive -> score -> package

The tests follow the recording-double style of ``tests/test_sweep.py``: a fake
orchestrator records the kwargs it receives, and the module-level seams
(``load_market_profile``, ``build_coverage``, ``build_dossier``,
``write_intel_package``) are monkeypatched to record what they were handed.
No network, no filesystem writes through the writer.
"""

from __future__ import annotations

import re
from datetime import date
from types import SimpleNamespace

import pytest

from src.core.config import Config
from src.core.models import Account
from src.pipeline import intel
from src.pipeline.runner import RunnerStats


DOMAIN = "acme.com"


# ---------------------------------------------------------------------------
# Recording doubles
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


class _DB:
    """Minimal jobs-table stand-in (query() returns rows as dicts)."""

    def __init__(self, rows=None):
        self.rows = list(rows or [])

    def query(self, sql, params=()):
        return list(self.rows)


class FakeOrch:
    """Fake orchestrator that records resolve/collect/score calls."""

    def __init__(self, *, accounts=None, jobs=None, snapshot=None):
        self.registry = _Registry(accounts)
        self.db = _DB(jobs)
        self.config = Config()
        self.calls = []
        self.snapshot = snapshot
        self.fail = set()

    def _record(self, name, kw):
        self.calls.append((name, kw))
        if name in self.fail:
            raise RuntimeError(f"{name} exploded")

    def resolve(self, **kw):
        self._record("resolve", kw)
        return {
            "accounts": 1, "cik": 0, "ats": 0, "feeds": 0, "icp": 0,
            "g2": 0, "appstore": 0, "bbb": 0, "linkedin": 0,
        }

    def collect(self, **kw):
        self._record("collect", kw)
        stats = RunnerStats()
        stats.tasks = 2
        stats.signals_new = 1
        stats.mark("dummy_source", DOMAIN, "ran_empty")
        return stats

    def score(self, **kw):
        self._record("score", kw)
        domain = list(kw["domains"])[0]
        snap = self.snapshot
        if snap is None:
            snap = SimpleNamespace(domain=domain, today=date(2026, 8, 16))
        if kw.get("return_snapshots"):
            return {"scored": 1, "snapshots": {domain: snap}}
        return {"scored": 1}

    def find_careers(self, domain):  # pragma: no cover - must never be called
        raise AssertionError("run_intel must not call find_careers")


class _Profile:
    def __init__(self, profile_id="default", *, empty=False):
        self.profile_id = profile_id
        self.seller = "Seller"
        self.offerings = ()
        self._empty = empty

    @property
    def is_empty(self):
        return self._empty


def _calls(orch, name):
    return [kw for n, kw in orch.calls if n == name]


def _patch_outputs(monkeypatch, *, rows=None, written=None):
    """Patch the package seams; return the recording dict."""
    rec = {}

    def fake_build_coverage(*, config, account, outcomes, opt_in_flags=None, requested=None):
        rec["coverage_kwargs"] = {
            "config": config,
            "account": account,
            "outcomes": list(outcomes),
            "opt_in_flags": dict(opt_in_flags or {}),
            "requested": requested,
        }
        return list(rows or [])

    def fake_build_dossier(snapshot, **kw):
        rec["dossier_snapshot"] = snapshot
        rec["dossier_kwargs"] = kw
        return {"domain": getattr(snapshot, "domain", None), "gaps": list(kw.get("gaps") or [])}

    def fake_write(dossier, *, out_dir):
        rec["write"] = {"dossier": dossier, "out_dir": out_dir}
        return dict(written or {"package_dir": "pkg"})

    monkeypatch.setattr(intel, "build_coverage", fake_build_coverage)
    monkeypatch.setattr(intel, "build_dossier", fake_build_dossier)
    monkeypatch.setattr(intel, "write_intel_package", fake_write)
    return rec


def _existing():
    return FakeOrch(accounts={DOMAIN: Account(domain=DOMAIN, name="Acme")})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_stage_order(monkeypatch):
    orch = _existing()
    _patch_outputs(monkeypatch)

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch)

    assert list(result["stages"]) == list(intel.STAGES)
    assert [result["stages"][s]["status"] for s in intel.STAGES] == ["ran"] * 5
    assert result["errors"] == {}


def test_identity_calls_resolve_once_with_default_flags(monkeypatch):
    orch = _existing()
    _patch_outputs(monkeypatch)

    intel.run_intel(DOMAIN, config=orch.config, orch=orch)

    calls = _calls(orch, "resolve")
    assert len(calls) == 1
    kw = calls[0]
    assert kw["domains"] == [DOMAIN]
    assert kw["ats"] is True
    assert kw["cik"] is True
    assert kw["feeds"] is True
    assert kw["icp"] is True
    assert kw["appstore"] is True
    assert kw["bbb"] is True
    assert kw["g2"] is False
    assert kw["linkedin"] is False
    # find_careers is never part of the flow (calling it would raise).
    assert "find_careers" not in [n for n, _ in orch.calls]


def test_opt_in_flags_reach_collect(monkeypatch):
    orch = _existing()
    _patch_outputs(monkeypatch)

    intel.run_intel(DOMAIN, config=orch.config, orch=orch, with_marketplaces=True)

    collects = _calls(orch, "collect")
    assert collects[0]["include_disabled_sources"] == set(intel.MARKETPLACE_SOURCE_KEYS)

    plain = _existing()
    _patch_outputs(monkeypatch)
    intel.run_intel(DOMAIN, config=plain.config, orch=plain)
    assert not _calls(plain, "collect")[0]["include_disabled_sources"]


def test_derive_restricted_to_local_sources_with_profile_context(monkeypatch):
    orch = _existing()
    _patch_outputs(monkeypatch)
    profile = _Profile()
    seen = {}

    def fake_load(profile_id, path=None):
        seen["profile_id"] = profile_id
        seen["path"] = path
        return profile

    monkeypatch.setattr(intel, "load_market_profile", fake_load)

    intel.run_intel(DOMAIN, config=orch.config, orch=orch)

    derive = _calls(orch, "collect")[1]
    assert derive["sources"] == ["jobsignals", "needs"]
    assert derive["force"] is True
    assert derive["local_context"]["market_profile"] is profile
    assert derive["local_context"]["market_profile_id"] == "default"
    assert seen["profile_id"] == "default"


def test_score_is_asked_for_snapshots_and_the_package_uses_that_snapshot(monkeypatch):
    sentinel = SimpleNamespace(domain=DOMAIN, today=date(2026, 8, 16))
    orch = FakeOrch(accounts={DOMAIN: Account(domain=DOMAIN, name="Acme")}, snapshot=sentinel)
    rec = _patch_outputs(monkeypatch)

    intel.run_intel(DOMAIN, config=orch.config, orch=orch)

    score = _calls(orch, "score")[0]
    assert score["return_snapshots"] is True
    assert score["domains"] == [DOMAIN]
    assert rec["dossier_snapshot"] is sentinel


def test_no_write_builds_dossier_but_never_writes(monkeypatch):
    orch = _existing()
    rec = _patch_outputs(monkeypatch)

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, write=False)

    assert result["dossier"] is not None
    assert "write" not in rec
    assert result["paths"] == {}


def test_unknown_profile_records_a_gap_and_continues(monkeypatch):
    orch = _existing()
    _patch_outputs(monkeypatch)

    result = intel.run_intel(
        DOMAIN, market_profile_id="does-not-exist", config=orch.config, orch=orch
    )

    assert any("does-not-exist" in gap for gap in result["gaps"])
    assert all(result["stages"][s]["status"] == "ran" for s in intel.STAGES)
    assert result["errors"] == {}


def test_stage_failure_is_recorded_and_package_is_skipped(monkeypatch):
    orch = _existing()
    orch.fail.add("score")
    _patch_outputs(monkeypatch)

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch)

    assert "score" in result["errors"]
    assert result["stages"]["score"]["status"] == "failed"
    assert result["stages"]["package"]["status"] == "skipped"
    assert "snapshot" in result["stages"]["package"]["note"].lower()
    assert result["dossier"] is None
    assert result["paths"] == {}


def test_bare_company_name_raises_value_error():
    with pytest.raises(ValueError):
        intel.run_intel("Glow")


def test_dry_run_requires_an_existing_account():
    orch = FakeOrch(accounts={})

    with pytest.raises(ValueError, match="dry-run requires an existing account"):
        intel.run_intel(DOMAIN, dry_run=True, config=orch.config, orch=orch)


def test_dry_run_never_fetches_or_writes(monkeypatch):
    orch = _existing()
    rec = _patch_outputs(monkeypatch)

    result = intel.run_intel(DOMAIN, dry_run=True, config=orch.config, orch=orch)

    assert _calls(orch, "resolve") == []
    assert _calls(orch, "score") == []
    assert "write" not in rec

    collects = _calls(orch, "collect")
    assert len(collects) == 1
    assert collects[0]["dry_run"] is True
    assert "planned_tasks" in result["stages"]["collect"]

    for stage in ("identity", "derive", "score", "package"):
        assert result["stages"][stage]["status"] == "skipped"
        assert "dry-run" in result["stages"][stage]["note"]


# ---------------------------------------------------------------------------
# Invocation id (defect found by the live run: the package carried none)
# ---------------------------------------------------------------------------

#: An 8-character lowercase hex id, exactly what uuid4().hex[:8] produces.
_INVOCATION_ID_RE = re.compile(r"[0-9a-f]{8}\Z")


def _patch_invocation_seams(monkeypatch, *, into_dossier: bool):
    """Patch the package seams; record the invocation id seen by each.

    ``into_dossier`` mirrors ``build_dossier``'s real contract of carrying the
    id into the dossier it returns.
    """
    rec = {"ids": [], "dossier": None}

    def recorder(snapshot, **kw):
        invocation_id = kw.get("invocation_id")
        rec["ids"].append(invocation_id)
        dossier = {"domain": getattr(snapshot, "domain", None)}
        if into_dossier:
            dossier["invocation_id"] = invocation_id
        return dossier

    def fake_write(dossier, *, out_dir):
        rec["dossier"] = dossier
        rec["out_dir"] = out_dir
        return {"package_dir": "pkg"}

    monkeypatch.setattr(intel, "build_coverage", lambda **kw: [])
    monkeypatch.setattr(intel, "build_dossier", recorder)
    monkeypatch.setattr(intel, "write_intel_package", fake_write)
    return rec


def test_run_intel_passes_a_unique_invocation_id(monkeypatch):
    rec = _patch_invocation_seams(monkeypatch, into_dossier=True)

    for _ in range(2):
        orch = _existing()
        intel.run_intel(DOMAIN, config=orch.config, orch=orch)

    assert len(rec["ids"]) == 2
    for invocation_id in rec["ids"]:
        assert isinstance(invocation_id, str), invocation_id
        assert _INVOCATION_ID_RE.match(invocation_id), invocation_id
    assert rec["ids"][0] != rec["ids"][1], "the invocation id must be per-run"


def test_package_directory_and_manifest_carry_the_invocation_id(monkeypatch):
    rec = _patch_invocation_seams(monkeypatch, into_dossier=True)
    orch = _existing()

    intel.run_intel(DOMAIN, config=orch.config, orch=orch)

    invocation_id = rec["ids"][0]
    assert _INVOCATION_ID_RE.match(invocation_id or ""), invocation_id
    assert rec["dossier"] is not None
    assert rec["dossier"]["invocation_id"] == invocation_id