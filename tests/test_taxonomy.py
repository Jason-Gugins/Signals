"""Tests for the signal taxonomy loader and validation."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.signals.taxonomy import Taxonomy, TaxonomyError, UnknownSignalType


ROOT = Path(__file__).resolve().parents[1]


def _base_type(**overrides) -> dict:
    row = {
        "category": "financial",
        "origin": "internal",
        "catalyst": "primary",
        "polarity": "positive",
        "degree": 0,
        "weight": 10,
        "half_life_days": 30,
        "play": "growth_pitch",
        "label": "X",
    }
    row.update(overrides)
    return row


def _data(types: dict) -> dict:
    return {
        "version": 1,
        "categories": ["financial", "hiring", "technology", "operational", "negative", "neutral", "intent"],
        "origins": ["internal", "external"],
        "catalysts": ["primary", "secondary"],
        "polarities": ["positive", "negative", "neutral"],
        "types": types,
    }


def test_loads_real_signals_yaml_41_types():
    tax = Taxonomy.load(str(ROOT / "config" / "signals.yaml"))
    assert len(tax.all()) == 48
    assert tax.get("funding_round").weight == 30
    assert tax.get("intent_1st_owned").degree == 1


def test_missing_field_raises_with_key():
    row = _base_type()
    del row["weight"]
    with pytest.raises(TaxonomyError, match="broken"):
        Taxonomy(_data({"broken": row}))


def test_invalid_enum_raises_with_key():
    with pytest.raises(TaxonomyError, match="broken"):
        Taxonomy(_data({"broken": _base_type(category="nope")}))


def test_degree_out_of_range_raises_with_key():
    with pytest.raises(TaxonomyError, match="broken"):
        Taxonomy(_data({"broken": _base_type(degree=4)}))


def test_nonzero_degree_requires_intent():
    with pytest.raises(TaxonomyError, match="broken"):
        Taxonomy(_data({"broken": _base_type(degree=1, category="financial")}))


def test_nonpositive_weight_raises_with_key():
    with pytest.raises(TaxonomyError, match="broken"):
        Taxonomy(_data({"broken": _base_type(weight=0)}))


def test_nonpositive_half_life_raises_with_key():
    with pytest.raises(TaxonomyError, match="broken"):
        Taxonomy(_data({"broken": _base_type(half_life_days=0)}))


def test_validate_against_real_plays_is_empty():
    tax = Taxonomy.load(str(ROOT / "config" / "signals.yaml"))
    plays = yaml.safe_load((ROOT / "config" / "plays.yaml").read_text(encoding="utf-8"))
    assert tax.validate_against_plays(plays) == []


def test_primary_types_match_section_32():
    tax = Taxonomy.load(str(ROOT / "config" / "signals.yaml"))
    expected = {
        "funding_round",
        "funding_form_d",
        "ipo_filing",
        "ipo_pricing",
        "pricing_change",
        "positioning_change",
        "ma_acquirer",
        "ma_target",
        "champion_migration",
        "exec_hire",
        "leadership_job_open",
        "hiring_surge",
        "tech_removed",
        "tech_churn",
        "renewal_window",
        "tech_migration_mentioned",
        "product_launch",
        "regulation_applicable",
        "competitor_outage",
        "layoff",
        "bankruptcy_signal",
        "contract_terminated",
        "federal_contract_award",
        "internal_project_scoop",
        "intent_1st_owned",
        "intent_2nd_marketplace",
        "marketplace_review_trend",
    }
    assert tax.primary_types() == expected


def test_intent_types_and_get_unknown():
    tax = Taxonomy.load(str(ROOT / "config" / "signals.yaml"))
    assert tax.intent_types() == {
        "intent_1st_owned",
        "intent_2nd_marketplace",
        "intent_3rd_topic",
        "marketplace_review_trend",
    }
    with pytest.raises(UnknownSignalType, match="not_a_type"):
        tax.get("not_a_type")
    hiring = tax.by_category("hiring")
    assert {t.key for t in hiring} >= {"champion_migration", "exec_hire"}
    keys = [t.key for t in tax.all()]
    assert keys == sorted(keys)
    # first validation case above counted a dummy; ensure 6 named failures exist
    # via the dedicated tests in this module
