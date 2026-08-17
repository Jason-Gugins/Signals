"""Tests for play mapping and template rendering."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml

from src.core.models import Account, Contact, Signal
from src.signals.plays import assign_plays, build_variables, render
from src.signals.score import Contribution, ScoreResult
from src.signals.taxonomy import Taxonomy
from src.signals.tier import TierResult


TODAY = date(2026, 8, 16)
TAX = Taxonomy.load("config/signals.yaml")
PLAYS = yaml.safe_load(Path("config/plays.yaml").read_text(encoding="utf-8"))
ACCT = Account(domain="acme.com", name="Acme", industry="Software", employee_count=200)


def _sig(typ, **data):
    spec = TAX.get(typ)
    return Signal(
        signal_id=typ,
        domain="acme.com",
        signal_type=typ,
        category=spec.category,
        origin=spec.origin,
        catalyst=spec.catalyst,
        polarity=spec.polarity,
        observed_at="2026-08-01",
        source="t",
        confidence=0.9,
        evidence_data=data,
    )


def test_funding_yields_growth_pitch_with_stage():
    sig = _sig("funding_round", round_stage="Series B")
    score = ScoreResult(40, 20, [Contribution("funding_round", "funding_round", "t", 30, 1, 0.9, 27)], [], 0, 1)
    tier = TierResult(2, "active", "x")
    plays = assign_plays(ACCT, [sig], score, tier, taxonomy=TAX, plays_cfg=PLAYS, contacts=[], today=TODAY)
    assert plays[0].play_id == "growth_pitch"
    assert "Series B" in plays[0].opener
    assert plays[0].opener
    assert plays[0].t24


def test_combo_outranks_higher_value_signal():
    fund = _sig("funding_round", round_stage="Series B")
    champ = _sig("champion_migration", new_role="CRO", prior_company="Gong")
    score = ScoreResult(
        80, 50,
        [
            Contribution("funding_round", "funding_round", "t", 30, 1, 0.9, 40),
            Contribution("champion_migration", "champion_migration", "t", 45, 1, 0.9, 20),
        ],
        [{"id": "super_signal", "bonus": 25, "urgency": 10, "action": "Call today.", "matched_signal_ids": ["champion_migration"]}],
        10, 1,
    )
    plays = assign_plays(ACCT, [fund, champ], score, TierResult(1, "active", "x"), taxonomy=TAX, plays_cfg=PLAYS, contacts=[], today=TODAY)
    assert plays[0].urgency >= 8


def test_missing_vars_no_braces_dedupe_and_cap():
    sig = _sig("funding_round")  # no round_stage
    vars_ = build_variables(ACCT, sig, None)
    text = render("Hello {first_name} at {company} about {round_stage}", vars_)
    assert "{" not in text
    assert "Acme" in text
    score = ScoreResult(
        50, 20,
        [
            Contribution("a", "funding_round", "t", 30, 1, 1, 20),
            Contribution("b", "funding_form_d", "t", 26, 1, 1, 18),
            Contribution("c", "award", "t", 6, 1, 1, 5),
            Contribution("d", "layoff", "t", 28, 1, 1, 15),
        ],
        [], 0, 1,
    )
    sigs = [_sig("funding_round"), _sig("funding_form_d"), _sig("award"), _sig("layoff")]
    plays = assign_plays(ACCT, sigs, score, TierResult(2, "active", "x"), taxonomy=TAX, plays_cfg=PLAYS, contacts=[], today=TODAY, max_plays=2)
    ids = [p.play_id for p in plays]
    assert len(ids) == len(set(ids))
    assert len(plays) <= 2


def test_tier4_gets_rapport_play():
    sig = _sig("award")
    score = ScoreResult(5, 1, [Contribution("award", "award", "t", 6, 1, 0.5, 3)], [], 0, 1)
    plays = assign_plays(ACCT, [sig], score, TierResult(4, "dormant", "x"), taxonomy=TAX, plays_cfg=PLAYS, contacts=[], today=TODAY)
    assert plays
    assert plays[0].play_id in {"flattery_opener", "ego_bait", "alignment_pitch"}
    contact = Contact(person_key="j", name="Jane Doe", title="CRO")
    v = build_variables(ACCT, sig, contact)
    assert v["first_name"] == "Jane"
    assert v["company"] == "Acme"
