"""Tests for the account registry and alias resolution."""

from __future__ import annotations

from src.core.db import Database
from src.core.models import Account
from src.identity.registry import AccountRegistry


def _reg(tmp_path) -> AccountRegistry:
    return AccountRegistry(Database(tmp_path / "signals.db"))


def test_upsert_then_get(tmp_path):
    reg = _reg(tmp_path)
    out = reg.upsert(Account(domain="gong.io", name="Gong", ticker="GONG"), source="csv")
    assert out.domain == "gong.io"
    got = reg.get("gong.io")
    assert got is not None
    assert got.name == "Gong"
    assert got.ticker == "GONG"


def test_second_upsert_does_not_null_existing(tmp_path):
    reg = _reg(tmp_path)
    reg.upsert(Account(domain="gong.io", name="Gong", ticker="GONG", industry="Software"))
    reg.upsert(Account(domain="gong.io", industry="Computer Software"))
    got = reg.get("gong.io")
    assert got.name == "Gong"
    assert got.ticker == "GONG"
    assert got.industry == "Computer Software"


def test_resolve_by_each_alias_kind(tmp_path):
    reg = _reg(tmp_path)
    reg.upsert(
        Account(
            domain="salesforce.com",
            name="Salesforce",
            linkedin_slug="salesforce",
            ticker="CRM",
            cik="0001108524",
            ats_token="salesforce",
        )
    )
    assert reg.resolve(domain="salesforce.com").domain == "salesforce.com"
    assert reg.resolve(linkedin_slug="salesforce").domain == "salesforce.com"
    assert reg.resolve(ticker="CRM").domain == "salesforce.com"
    assert reg.resolve(cik="0001108524").domain == "salesforce.com"
    assert reg.resolve(url="https://www.salesforce.com/products").domain == "salesforce.com"
    assert reg.resolve(name="Salesforce").domain == "salesforce.com"


def test_fuzzy_resolve_unique_hit_and_ambiguity(tmp_path):
    reg = _reg(tmp_path)
    reg.upsert(Account(domain="gong.io", name="Gong"))
    assert reg.resolve(name="Gong").domain == "gong.io"
    # no exact alias for this string; unique fuzzy hit
    assert reg.resolve(name="Gong Incorporated", min_similarity=0.5).domain == "gong.io"
    reg.upsert(Account(domain="acme-soft.com", name="Acme Software"))
    reg.upsert(Account(domain="acme-robo.com", name="Acme Robotics"))
    assert reg.resolve(name="Acme", min_similarity=0.4) is None
    assert reg.resolve(name="Completely Unknown Co") is None


def test_alias_kinds_do_not_cross_match(tmp_path):
    reg = _reg(tmp_path)
    reg.upsert(Account(domain="salesforce.com", ticker="CRM"))
    reg.upsert(Account(domain="crm-inc.com", name="CRM"))
    assert reg.resolve(ticker="CRM").domain == "salesforce.com"
    assert reg.resolve(name="CRM").domain == "crm-inc.com"
    assert reg.resolve(ticker="CRM", name="CRM").domain == "salesforce.com"


def test_list_accounts_order_and_cohort(tmp_path):
    reg = _reg(tmp_path)
    reg.upsert(Account(domain="a.com", name="A", cohort="ca", score=10))
    reg.upsert(Account(domain="b.com", name="B", cohort="ca", score=50))
    reg.upsert(Account(domain="c.com", name="C", cohort="us", score=90))
    ca = reg.list_accounts(cohort="ca")
    assert [a.domain for a in ca] == ["b.com", "a.com"]
    top = reg.list_accounts(limit=1)
    assert top[0].domain == "c.com"
    assert reg.count(cohort="ca") == 2
    assert reg.count() == 3


def test_set_scores(tmp_path):
    reg = _reg(tmp_path)
    reg.upsert(Account(domain="a.com", name="A"))
    reg.set_scores("a.com", score=71.5, tier=1, buying_window="active", scored_at="2026-08-16")
    got = reg.get("a.com")
    assert got.score == 71.5
    assert got.tier == 1
    assert got.buying_window == "active"
    assert got.scored_at == "2026-08-16"
