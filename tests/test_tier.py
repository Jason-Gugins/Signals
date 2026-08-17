"""Tests for HPP tiering and buying-window state machine."""

from __future__ import annotations

from datetime import date, timedelta

from src.core.models import Signal
from src.signals.score import ScoreResult
from src.signals.taxonomy import Taxonomy
from src.signals.tier import assign_tier, buying_window, newest_primary


TODAY = date(2026, 8, 16)
TAX = Taxonomy.load("config/signals.yaml")
CFG = {
    "tiers": {"tier1": {"min_score": 70, "or_urgency": 8}, "tier2": {"min_score": 45}, "tier3": {"min_score": 20}},
    "buying_window": {"active_days": 30, "opening_days": 90, "developing_days": 180},
}


def _sig(typ: str, days_ago: int) -> Signal:
    spec = TAX.get(typ)
    return Signal(
        signal_id=f"{typ}-{days_ago}",
        domain="acme.com",
        signal_type=typ,
        category=spec.category,
        origin=spec.origin,
        catalyst=spec.catalyst,
        polarity=spec.polarity,
        observed_at=(TODAY - timedelta(days=days_ago)).isoformat(),
        source="t",
        degree=spec.degree,
        confidence=0.9,
    )


def _score(score=0.0, urgency=0) -> ScoreResult:
    return ScoreResult(score=score, raw=0, contributions=[], combos=[], urgency=urgency, icp_multiplier=1.0)


def test_tier1_paths():
    # primary internal + external
    sigs = [_sig("exec_hire", 6), _sig("regulation_applicable", 41)]
    r = assign_tier(sigs, _score(10), taxonomy=TAX, cfg=CFG, today=TODAY)
    assert r.tier == 1
    assert "primary" in r.rationale.lower() or "exec_hire" in r.rationale
    assert r.buying_window == "active"
    # urgency override
    r2 = assign_tier([_sig("award", 10)], _score(10, urgency=8), taxonomy=TAX, cfg=CFG, today=TODAY)
    assert r2.tier == 1
    # score override
    r3 = assign_tier([_sig("award", 10)], _score(70), taxonomy=TAX, cfg=CFG, today=TODAY)
    assert r3.tier == 1


def test_tier2_paths():
    r = assign_tier([_sig("exec_hire", 60)], _score(10), taxonomy=TAX, cfg=CFG, today=TODAY)
    assert r.tier == 2
    assert r.buying_window == "opening"
    r2 = assign_tier([_sig("award", 10)], _score(50), taxonomy=TAX, cfg=CFG, today=TODAY)
    assert r2.tier == 2


def test_tier3_and_tier4():
    only_ext = [_sig("intent_3rd_topic", 20)]
    r = assign_tier(only_ext, _score(10), taxonomy=TAX, cfg=CFG, today=TODAY)
    assert r.tier == 3
    r2 = assign_tier([_sig("award", 10)], _score(25), taxonomy=TAX, cfg=CFG, today=TODAY)
    assert r2.tier == 3
    empty = assign_tier([], _score(0), taxonomy=TAX, cfg=CFG, today=TODAY)
    assert empty.tier == 4 and empty.buying_window == "dormant"
    old = assign_tier([_sig("award", 200)], _score(5), taxonomy=TAX, cfg=CFG, today=TODAY)
    assert old.tier == 4 and old.buying_window == "dormant"


def test_window_boundaries():
    assert buying_window([_sig("exec_hire", 30)], taxonomy=TAX, cfg=CFG, today=TODAY) == "active"
    assert buying_window([_sig("exec_hire", 31)], taxonomy=TAX, cfg=CFG, today=TODAY) == "opening"
    assert buying_window([_sig("exec_hire", 90)], taxonomy=TAX, cfg=CFG, today=TODAY) == "opening"
    assert buying_window([_sig("award", 91)], taxonomy=TAX, cfg=CFG, today=TODAY) == "developing"
    assert buying_window([_sig("award", 180)], taxonomy=TAX, cfg=CFG, today=TODAY) == "developing"
    assert buying_window([_sig("award", 181)], taxonomy=TAX, cfg=CFG, today=TODAY) == "dormant"


def test_newest_primary():
    sigs = [_sig("exec_hire", 40), _sig("funding_round", 10), _sig("award", 1)]
    n = newest_primary(sigs, taxonomy=TAX)
    assert n is not None and n.signal_type == "funding_round"
    assert newest_primary([_sig("award", 1)], taxonomy=TAX) is None
