"""Tests for stacking combo evaluation."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from src.core.config import ConfigError
from src.core.models import Signal
from src.signals.combos import condition_matches, evaluate_combos
from src.signals.taxonomy import Taxonomy


TODAY = date(2026, 8, 16)
TAX = Taxonomy.load("config/signals.yaml")
COMBOS = yaml.safe_load(Path("config/scoring.yaml").read_text(encoding="utf-8"))["combos"]


def _sig(typ: str, days_ago: int, sid: str | None = None, conf: float = 0.9) -> Signal:
    spec = TAX.get(typ)
    observed = (TODAY - timedelta(days=days_ago)).isoformat()
    return Signal(
        signal_id=sid or f"{typ}-{days_ago}",
        domain="acme.com",
        signal_type=typ,
        category=spec.category,
        origin=spec.origin,
        catalyst=spec.catalyst,
        polarity=spec.polarity,
        observed_at=observed,
        source="t",
        confidence=conf,
    )


MINIMAL = {
    "super_signal": [_sig("exec_hire", 6), _sig("intent_1st_owned", 2)],
    "champion_play": [_sig("champion_migration", 10)],
    "deep_pockets": [_sig("funding_round", 30), _sig("hiring_surge", 10)],
    "marketplace": [_sig("intent_2nd_marketplace", 5)],
    "efficiency_pivot": [_sig("layoff", 10)],
    "displacement_clock": [_sig("renewal_window", 20), _sig("competitor_detected", 40)],
    "contextual_cold": [_sig("tech_install_new", 15)],
    "turnaround_pitch": [_sig("intent_2nd_marketplace", 5), _sig("exec_departure", 20)],
    "relocation_window": [_sig("new_geo", 10), _sig("layoff", 40)],
}

TOO_OLD = {
    "super_signal": [_sig("exec_hire", 31), _sig("intent_1st_owned", 2)],
    "champion_play": [_sig("champion_migration", 31)],
    "deep_pockets": [_sig("funding_round", 121), _sig("hiring_surge", 10)],
    "marketplace": [_sig("intent_2nd_marketplace", 31)],
    "efficiency_pivot": [_sig("layoff", 61)],
    "displacement_clock": [_sig("renewal_window", 91), _sig("competitor_detected", 40)],
    "contextual_cold": [_sig("tech_install_new", 91)],
    "turnaround_pitch": [_sig("intent_2nd_marketplace", 31), _sig("exec_departure", 20)],
    "relocation_window": [_sig("new_geo", 31), _sig("layoff", 40)],
}


def test_each_combo_fires_on_minimal_set():
    ids = {c["id"] for c in COMBOS}
    assert ids == set(MINIMAL)
    for cid, sigs in MINIMAL.items():
        fired = evaluate_combos(sigs, COMBOS, today=TODAY)
        assert any(f["id"] == cid for f in fired), cid
        assert fired[0].get("matched_signal_ids")


def test_one_day_too_old_does_not_fire():
    for cid, sigs in TOO_OLD.items():
        fired = {f["id"] for f in evaluate_combos(sigs, COMBOS, today=TODAY)}
        assert cid not in fired, cid


def test_none_of_suppresses_efficiency_pivot():
    sigs = [_sig("layoff", 10), _sig("funding_round", 30)]
    fired = {f["id"] for f in evaluate_combos(sigs, COMBOS, today=TODAY)}
    assert "efficiency_pivot" not in fired


def test_min_count_and_urgency_order():
    defs = [
        {
            "id": "need_two",
            "bonus": 1,
            "urgency": 3,
            "action": "x",
            "all_of": [{"any_type": ["award"], "within_days": 30, "min_count": 2}],
        },
        {
            "id": "hi",
            "bonus": 1,
            "urgency": 9,
            "action": "y",
            "all_of": [{"any_type": ["award"], "within_days": 30}],
        },
    ]
    one = [_sig("award", 1)]
    assert [f["id"] for f in evaluate_combos(one, defs, today=TODAY)] == ["hi"]
    two = [_sig("award", 1, "a1"), _sig("award", 2, "a2")]
    ids = [f["id"] for f in evaluate_combos(two, defs, today=TODAY)]
    assert ids[0] == "hi"
    assert "need_two" in ids


def test_unknown_condition_key_raises():
    defs = [{"id": "bad", "bonus": 1, "urgency": 1, "action": "z", "all_of": [{"nope": True}]}]
    with pytest.raises(ConfigError):
        evaluate_combos([], defs, today=TODAY)


NEW_RECIPES = ("turnaround_pitch", "relocation_window")


def test_new_recipe_combos_fire_within_window():
    """Turnaround + relocation combos fire when both clauses are in-window."""
    fired = {f["id"] for f in evaluate_combos(
        MINIMAL["turnaround_pitch"] + MINIMAL["relocation_window"], COMBOS, today=TODAY)}
    for cid in NEW_RECIPES:
        assert cid in fired, cid


def test_new_recipe_combos_do_not_fire_when_clause_is_stale():
    """Each new combo stays silent when any clause signal is outside within_days."""
    for cid in NEW_RECIPES:
        fired = {f["id"] for f in evaluate_combos(TOO_OLD[cid], COMBOS, today=TODAY)}
        assert cid not in fired, cid


def test_new_recipe_ids_and_actions_in_loaded_config():
    """New recipe ids exist in config with non-empty action text."""
    by_id = {c["id"]: c for c in COMBOS}
    for cid in NEW_RECIPES:
        assert cid in by_id, cid
        entry = by_id[cid]
        assert entry.get("action"), cid
        assert int(entry.get("bonus") or 0) > 0
        assert int(entry.get("urgency") or 0) > 0
    # honest-proxy note in the turnaround action (no sentiment_decline type exists)
    assert "proxy" in by_id["turnaround_pitch"]["action"].lower()
    # verify-first caveat in the relocation action (region matching not automatable)
    assert "verify" in by_id["relocation_window"]["action"].lower()
