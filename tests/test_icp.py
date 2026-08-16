"""Tests for ICP fit evaluation."""

from __future__ import annotations

import pytest

from src.core.config import ConfigError
from src.core.models import Account, Signal
from src.identity.icp import evaluate_icp


RULES = {
    "rules": [
        {
            "id": "size_sweet_spot",
            "when": {"employee_count_between": [50, 2000]},
            "multiplier": 1.25,
            "reason": "Headcount in ICP band",
        },
        {
            "id": "too_small",
            "when": {"employee_count_lt": 20},
            "multiplier": 0.5,
            "reason": "Below minimum viable size",
        },
        {
            "id": "geo",
            "when": {"hq_country_any": ["Canada", "United States"]},
            "multiplier": 1.1,
            "reason": "Serviceable geography",
        },
    ],
    "disqualifiers": [
        {
            "id": "competitor",
            "when": {"domain_in_file": "WILL_SET"},
            "reason": "Competitor",
        }
    ],
}


def test_multiplier_is_product_and_clamped(tmp_path):
    acct = Account(domain="acme.com", employee_count=100, hq_country="Canada")
    rules = {**RULES, "disqualifiers": []}
    result = evaluate_icp(acct, rules)
    assert result.disqualified is False
    assert abs(result.multiplier - 1.25 * 1.1) < 1e-9
    assert "Headcount in ICP band" in result.reasons
    assert "Serviceable geography" in result.reasons

    huge = evaluate_icp(
        Account(domain="x.com", employee_count=100, hq_country="Canada"),
        {
            "rules": [
                {"id": "a", "when": {"employee_count_gt": 1}, "multiplier": 3.0, "reason": "a"},
                {"id": "b", "when": {"employee_count_gt": 1}, "multiplier": 3.0, "reason": "b"},
            ],
            "disqualifiers": [],
        },
    )
    assert huge.multiplier == 2.0


def test_disqualifier_short_circuits(tmp_path):
    p = tmp_path / "competitors.txt"
    p.write_text("rival.com\n", encoding="utf-8")
    rules = {
        "rules": RULES["rules"],
        "disqualifiers": [
            {"id": "competitor", "when": {"domain_in_file": str(p)}, "reason": "Competitor"}
        ],
    }
    result = evaluate_icp(Account(domain="rival.com", employee_count=200), rules)
    assert result.disqualified is True
    assert result.disqualify_reason == "Competitor"
    assert result.multiplier == 0.0


def test_unknown_predicate_raises():
    rules = {
        "rules": [{"id": "x", "when": {"not_a_real_pred": True}, "multiplier": 1.0, "reason": "x"}],
        "disqualifiers": [],
    }
    with pytest.raises(ConfigError):
        evaluate_icp(Account(domain="a.com"), rules)


def test_has_signal_type_and_department(tmp_path):
    rules = {
        "rules": [
            {
                "id": "has_sales_org",
                "when": {"has_signal_type": "hiring_surge", "department_any": ["Sales"]},
                "multiplier": 1.15,
                "reason": "Active sales org growth",
            }
        ],
        "disqualifiers": [],
    }
    sig = Signal(
        signal_id="1",
        domain="a.com",
        signal_type="hiring_surge",
        category="hiring",
        origin="internal",
        catalyst="primary",
        polarity="positive",
        observed_at="2026-08-01",
        source="jobs",
        evidence_data={"department": "Sales"},
    )
    result = evaluate_icp(Account(domain="a.com"), rules, signals=[sig])
    assert abs(result.multiplier - 1.15) < 1e-9
    miss = evaluate_icp(Account(domain="a.com"), rules, signals=[])
    assert miss.multiplier == 1.0
