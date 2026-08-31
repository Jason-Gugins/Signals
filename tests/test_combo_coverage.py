"""Combo coverage validator tests (Task 4, P2 roadmap)."""

from __future__ import annotations

from click.testing import CliRunner

from src.cli import main
from src.signals.plays import validate_combo_coverage


class _FakeConfig:
    def __init__(self, combos):
        self._combos = combos

    def load_yaml(self, name):
        assert name == "scoring"
        return {"combos": self._combos}


def _patch_config_load(monkeypatch, combos):
    from src.core.config import Config

    monkeypatch.setattr(
        Config, "load", classmethod(lambda cls, *a, **k: _FakeConfig(combos))
    )


def test_shipped_config_is_fully_covered(monkeypatch):
    """The shipped scoring.yaml combos all have COMBO_PLAY entries."""
    _patch_config_load(monkeypatch, None)  # not used; force default path instead
    monkeypatch.undo()
    assert validate_combo_coverage() == []


def test_fake_uncovered_id_is_reported(monkeypatch):
    """A combo id absent from COMBO_PLAY is returned by the validator."""
    combos = [
        {"id": "super_signal"},
        {"id": "fake_combo"},
    ]
    _patch_config_load(monkeypatch, combos)
    assert validate_combo_coverage() == ["fake_combo"]


def test_doctor_warns_on_gaps(monkeypatch):
    """Doctor prints a warning line when the validator finds gaps; exit code stays 0."""
    import src.pipeline.health as health_mod
    import src.signals.plays as plays_mod

    monkeypatch.setattr(health_mod, "doctor", lambda config, db, **kw: [("python", "OK", "3.12")])
    monkeypatch.setattr(plays_mod, "validate_combo_coverage", lambda *a, **k: ["fake_combo"])

    class _NoDB:
        def __init__(self, *a, **k):
            pass

    import src.cli as cli_mod

    monkeypatch.setattr(cli_mod, "Database", _NoDB)

    runner = CliRunner()
    result = runner.invoke(main, ["doctor", "--no-network"])
    assert result.exit_code == 0
    assert "combo coverage gaps: fake_combo" in result.output


def test_doctor_silent_when_covered(monkeypatch):
    """No warning line when every combo is covered."""
    import src.pipeline.health as health_mod
    import src.signals.plays as plays_mod

    monkeypatch.setattr(health_mod, "doctor", lambda config, db, **kw: [])
    monkeypatch.setattr(plays_mod, "validate_combo_coverage", lambda *a, **k: [])

    class _NoDB:
        def __init__(self, *a, **k):
            pass

    import src.cli as cli_mod

    monkeypatch.setattr(cli_mod, "Database", _NoDB)

    runner = CliRunner()
    result = runner.invoke(main, ["doctor", "--no-network"])
    assert result.exit_code == 0
    assert "combo coverage gaps" not in result.output
