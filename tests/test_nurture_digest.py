"""Task 15 (digest part): tier-4 nurture digest — --tier-4 flag wiring."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from src.cli import _digest_paths
from src.core.models import Account
from src.signals.score import ScoreResult
from src.signals.tier import TierResult

TODAY = date(2026, 9, 2)


class _Store:
    def for_account(self, domain):
        return []


class _FakeOrch:
    def __init__(self, accounts):
        self._accounts_list = accounts
        self.db = object()
        self.signal_store = _Store()
        self.taxonomy = None

    def _accounts(self, *, domains=None, cohort=None, **kw):
        return self._accounts_list

    def _contacts(self, domain):
        return []


class _FakeCfg:
    def __init__(self, digests_dir):
        self._d = digests_dir

    def load_yaml(self, name):
        return {}

    class storage:
        pass


class _Storage:
    def __init__(self, d):
        self.digests_dir = str(d)


def _ctx(orch, tmp_path):
    cfg = _FakeCfg(tmp_path)
    cfg.storage = _Storage(tmp_path)
    from types import SimpleNamespace

    return SimpleNamespace(obj={"get_orch": lambda: orch, "config": cfg, "cohort": None})


def _wire(monkeypatch, tmp_path, tier):
    """Patch scoring internals so the digest path is deterministic."""
    import src.cli as cli
    import src.export.digest as digest_mod
    import src.signals.plays as plays_mod
    import src.signals.tier as tier_mod

    captured = {}

    def fake_assign_tier(signals, result, **kw):
        return TierResult(tier, "dormant", f"Tier {tier}.")

    def fake_assign_plays(account, signals, result, t, **kw):
        return [{"play_id": "growth_pitch"}]

    def fake_build_digest(domain, signals, plays, *, period, taxonomy=None, since=None):
        captured[domain] = plays
        return f"# {domain} — {period} digest\n"

    monkeypatch.setattr(tier_mod, "assign_tier", fake_assign_tier)
    monkeypatch.setattr(plays_mod, "assign_plays", fake_assign_plays)
    monkeypatch.setattr(digest_mod, "build_digest", fake_build_digest)
    # _digest_paths imports these names from their source modules at call time
    monkeypatch.setattr(cli, "_today", lambda: TODAY, raising=False)
    return captured


def _account():
    return Account(domain="dormant.example", name="Dormant Co")


def test_tier4_flag_included_no_plays(monkeypatch, tmp_path):
    import src.pipeline.orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "_today", lambda: TODAY)
    monkeypatch.setattr("src.signals.calibration.load_stats", lambda db: {})
    captured = _wire(monkeypatch, tmp_path, tier=4)
    orch = _FakeOrch([_account()])
    paths = _digest_paths(_ctx(orch, tmp_path), "daily", (), include_tier4=True)
    assert len(paths) == 1
    assert Path(paths[0]).exists()
    assert captured["dormant.example"] == []


def test_tier4_default_off_unchanged(monkeypatch, tmp_path):
    import src.pipeline.orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "_today", lambda: TODAY)
    monkeypatch.setattr("src.signals.calibration.load_stats", lambda db: {})
    captured = _wire(monkeypatch, tmp_path, tier=4)
    orch = _FakeOrch([_account()])
    _digest_paths(_ctx(orch, tmp_path), "daily", ())
    # Default (flag off): plays computed as before, not forced empty.
    assert captured["dormant.example"] == [{"play_id": "growth_pitch"}]


def test_tier4_non_tier4_unaffected(monkeypatch, tmp_path):
    import src.pipeline.orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "_today", lambda: TODAY)
    monkeypatch.setattr("src.signals.calibration.load_stats", lambda db: {})
    captured = _wire(monkeypatch, tmp_path, tier=2)
    orch = _FakeOrch([_account()])
    _digest_paths(_ctx(orch, tmp_path), "daily", (), include_tier4=True)
    assert captured["dormant.example"] == [{"play_id": "growth_pitch"}]
