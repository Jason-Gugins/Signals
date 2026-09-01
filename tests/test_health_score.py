"""Tests for account health score (signal polarity aggregation) and play gating."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml

from src.core.models import Account, Signal
from src.signals.health import health_score
from src.signals.plays import assign_plays
from src.signals.score import Contribution, ScoreResult
from src.signals.taxonomy import Taxonomy
from src.signals.tier import TierResult


TODAY = date(2026, 8, 16)
TAX = Taxonomy.load("config/signals.yaml")
PLAYS = yaml.safe_load(Path("config/plays.yaml").read_text(encoding="utf-8"))
ACCT = Account(domain="acme.com", name="Acme", industry="Software", employee_count=200)


def _sig(typ: str, signal_id: str | None = None) -> Signal:
    spec = TAX.get(typ)
    return Signal(
        signal_id=signal_id or typ,
        domain="acme.com",
        signal_type=typ,
        category=spec.category,
        origin=spec.origin,
        catalyst=spec.catalyst,
        polarity=spec.polarity,
        observed_at="2026-08-01",
        source="t",
        confidence=0.9,
        evidence_data={},
    )


def _fund_score() -> ScoreResult:
    """A score whose top contribution maps to growth_pitch."""
    return ScoreResult(
        40,
        20,
        [Contribution("funding_round", "funding_round", "t", 30, 1, 0.9, 27)],
        [],
        0,
        1,
    )


# --- health_score: polarity math -------------------------------------------


def test_two_positive_one_negative_is_positive():
    score, reasons = health_score([_sig("funding_round"), _sig("exec_hire"), _sig("layoff")])
    assert score > 0
    assert score <= 1.0
    assert any(r.startswith("layoff:") for r in reasons)
    assert any(r.startswith("funding_round:") for r in reasons)


def test_warn_plus_layoff_is_negative():
    score, reasons = health_score([_sig("earnings_warning"), _sig("layoff")])
    assert score < 0
    assert score >= -1.0
    assert len(reasons) == 2


def test_all_negative_saturates_at_minus_one():
    score, _ = health_score([_sig("layoff"), _sig("layoff", "layoff2"), _sig("earnings_warning")])
    assert score == -1.0


def test_all_positive_saturates_at_plus_one():
    score, _ = health_score([_sig("funding_round"), _sig("exec_hire"), _sig("product_launch")])
    assert score == 1.0


def test_unknown_signal_type_is_neutral():
    # Types absent from the polarity mapping contribute nothing.
    score, reasons = health_score([_sig("competitor_detected"), _sig("content_appearance")])
    assert score == 0.0
    assert reasons == []


def test_empty_signals_is_neutral():
    assert health_score([]) == (0.0, [])


def test_weights_override_changes_score():
    sigs = [_sig("layoff")]
    base, base_reasons = health_score(sigs)
    assert base < 0
    assert base_reasons == ["layoff: -1.5"]
    override, override_reasons = health_score(sigs, weights={"layoff": 2.0})
    assert override < base
    assert override_reasons == ["layoff: -2.0"]


def test_weights_override_none_uses_defaults():
    # No config file involved: weights=None must fall back to built-in defaults
    # (layoff weighs 1.5; single signal → -1.5/3.0 normalized).
    score, reasons = health_score([_sig("layoff")], weights=None)
    assert score == -0.5
    assert reasons == ["layoff: -1.5"]


# --- assign_plays: health gate ---------------------------------------------


def test_layoff_account_gets_no_growth_pitch():
    # Health = (-1.5 - 1.5) / 3.0 = -1.0 < threshold -0.5 → growth_pitch suppressed.
    sigs = [_sig("layoff"), _sig("earnings_warning")]
    plays = assign_plays(
        ACCT, sigs, _fund_score(), TierResult(2, "active", "x"),
        taxonomy=TAX, plays_cfg=PLAYS, contacts=[], today=TODAY,
    )
    assert "growth_pitch" not in [p.play_id for p in plays]


def test_healthy_account_still_gets_growth_pitch():
    plays = assign_plays(
        ACCT, [_sig("funding_round")], _fund_score(), TierResult(2, "active", "x"),
        taxonomy=TAX, plays_cfg=PLAYS, contacts=[], today=TODAY,
    )
    assert plays
    assert plays[0].play_id == "growth_pitch"


def test_gate_threshold_configurable_via_plays_cfg():
    # With threshold raised to 0.0, a single layoff (health = -1.0) gates the account.
    cfg = dict(PLAYS)
    cfg["health_gate"] = {"threshold": 0.0, "suppress_plays": ["growth_pitch"]}
    plays = assign_plays(
        ACCT, [_sig("layoff")], _fund_score(), TierResult(2, "active", "x"),
        taxonomy=TAX, plays_cfg=cfg, contacts=[], today=TODAY,
    )
    assert "growth_pitch" not in [p.play_id for p in plays]


def test_gate_suppress_list_configurable():
    # Suppressing a different family leaves growth_pitch alone even at bad health.
    cfg = dict(PLAYS)
    cfg["health_gate"] = {"threshold": -0.5, "suppress_plays": ["automation_pitch"]}
    plays = assign_plays(
        ACCT, [_sig("layoff"), _sig("earnings_warning")], _fund_score(), TierResult(2, "active", "x"),
        taxonomy=TAX, plays_cfg=cfg, contacts=[], today=TODAY,
    )
    assert plays
    assert plays[0].play_id == "growth_pitch"
