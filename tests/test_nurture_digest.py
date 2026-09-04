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

    def fake_build_digest(domain, signals, plays, *, period, taxonomy=None, since=None, **kw):
        captured[domain] = plays
        return f"# {domain} — {period} digest\n"

    monkeypatch.setattr(tier_mod, "assign_tier", fake_assign_tier)
    monkeypatch.setattr(plays_mod, "assign_plays", fake_assign_plays)
    monkeypatch.setattr(digest_mod, "build_digest", fake_build_digest)
    # _digest_paths imports these names from their source modules at call time
    monkeypatch.setattr(cli, "_today", lambda: TODAY, raising=False)
    # score_account needs a taxonomy; the fake orch has None. Patch it.
    from types import SimpleNamespace

    monkeypatch.setattr("src.signals.score.score_account", lambda acct, signals, **kw: SimpleNamespace())
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


# ---------------------------------------------------------------------------
# P3 Batch 3+4 review fixes: MAJOR 1 (flag reaches _digest_paths) and
# MAJOR 2 (supersede partition on the digest path).
# ---------------------------------------------------------------------------


def test_digest_cli_tier4_flag_reaches_digest_paths(monkeypatch):
    """MAJOR 1: the --tier-4 flag must reach _digest_paths (was dropped on the floor)."""
    import src.cli as cli
    from click.testing import CliRunner

    seen = {}

    def fake_digest_paths(ctx, period, domains, include_tier4=False):
        seen["include_tier4"] = include_tier4
        return []

    monkeypatch.setattr(cli, "_digest_paths", fake_digest_paths)
    runner = CliRunner()
    assert runner.invoke(cli.main, ["digest", "--tier-4"]).exit_code == 0
    assert seen["include_tier4"] is True
    # Without the flag the kwarg stays False.
    assert runner.invoke(cli.main, ["digest"]).exit_code == 0
    assert seen["include_tier4"] is False


def test_expired_signal_excluded_from_digest_signal_groups(monkeypatch, tmp_path):
    """MAJOR 2: expired (superseded) signals must not reach build_digest."""
    from datetime import timedelta

    import src.export.digest as digest_mod
    import src.pipeline.orchestrator as orch_mod
    from src.core.models import Signal

    monkeypatch.setattr(orch_mod, "_today", lambda: TODAY)
    monkeypatch.setattr("src.signals.calibration.load_stats", lambda db: {})

    seen_signals = {}

    def fake_build_digest(domain, signals, plays, *, period, taxonomy=None, since=None, **kw):
        seen_signals[domain] = list(signals)
        return f"# {domain} — {period} digest\n"

    monkeypatch.setattr(digest_mod, "build_digest", fake_build_digest)
    monkeypatch.setattr("src.signals.tier.assign_tier", lambda s, r, **kw: TierResult(2, "opening", "T2."))
    monkeypatch.setattr("src.signals.plays.assign_plays", lambda a, s, r, t, **kw: [])

    old_observed = (TODAY - timedelta(days=30)).isoformat()
    expired = Signal(
        signal_id="s1", domain="dormant.example", signal_type="funding_round",
        category="growth", origin="tech", catalyst="hiring", polarity="positive",
        observed_at=old_observed, source="test",
    )

    class _Store1:
        def for_account(self, domain):
            return [expired]

    orch = _FakeOrch([_account()])
    orch.signal_store = _Store1()

    cfg = _FakeCfg(tmp_path)
    cfg.storage = _Storage(tmp_path)

    def load_yaml(name):
        if name == "signals":
            return {"types": {"funding_round": {"supersede_days": 7}}}
        return {}

    cfg.load_yaml = load_yaml
    from types import SimpleNamespace

    ctx = SimpleNamespace(obj={"get_orch": lambda: orch, "config": cfg, "cohort": None})
    paths = _digest_paths(ctx, "daily", ())
    assert len(paths) == 1 and Path(paths[0]).exists()
    assert seen_signals["dormant.example"] == []


def test_expired_signal_included_when_no_supersede_map(monkeypatch, tmp_path):
    """MAJOR 2: with an empty supersede map the digest path is unchanged."""
    from datetime import timedelta

    import src.export.digest as digest_mod
    import src.pipeline.orchestrator as orch_mod
    from src.core.models import Signal

    monkeypatch.setattr(orch_mod, "_today", lambda: TODAY)
    monkeypatch.setattr("src.signals.calibration.load_stats", lambda db: {})
    # score_account needs a taxonomy; the fake orch has None.
    from types import SimpleNamespace

    monkeypatch.setattr("src.signals.score.score_account", lambda acct, signals, **kw: SimpleNamespace())

    seen_signals = {}

    def fake_build_digest(domain, signals, plays, *, period, taxonomy=None, since=None, **kw):
        seen_signals[domain] = list(signals)
        return f"# {domain} — {period} digest\n"

    monkeypatch.setattr(digest_mod, "build_digest", fake_build_digest)
    monkeypatch.setattr("src.signals.tier.assign_tier", lambda s, r, **kw: TierResult(2, "opening", "T2."))
    monkeypatch.setattr("src.signals.plays.assign_plays", lambda a, s, r, t, **kw: [])

    old_observed = (TODAY - timedelta(days=30)).isoformat()
    sig = Signal(
        signal_id="s1", domain="dormant.example", signal_type="funding_round",
        category="growth", origin="tech", catalyst="hiring", polarity="positive",
        observed_at=old_observed, source="test",
    )

    class _Store1:
        def for_account(self, domain):
            return [sig]

    orch = _FakeOrch([_account()])
    orch.signal_store = _Store1()
    # _FakeCfg.load_yaml returns {} for every key -> empty supersede map.
    paths = _digest_paths(_ctx(orch, tmp_path), "daily", ())
    assert len(paths) == 1
    assert len(seen_signals["dormant.example"]) == 1


def test_load_supersede_map_skips_non_numeric():
    """MINOR C: non-numeric supersede_days is skipped with a warning, not raised."""
    from src.signals.lifecycle import load_supersede_map

    cfg = {
        "types": {
            "funding_round": {"supersede_days": 7},
            "hiring": {"supersede_days": "soon"},
            "leadership_change": {"supersede_days": None},
            "product_launch": {"supersede_days": []},
        }
    }
    assert load_supersede_map(cfg) == {"funding_round": 7}
