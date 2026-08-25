"""Tests for the g2_slug field on the Account model."""

from __future__ import annotations

from src.core.models import Account


def test_account_has_g2_slug_field():
    acct = Account(domain="acme.com", g2_slug="acme-crm")
    assert acct.g2_slug == "acme-crm"


def test_account_g2_slug_defaults_none():
    acct = Account(domain="acme.com")
    assert acct.g2_slug is None


def test_account_to_db_row_includes_g2_slug():
    acct = Account(domain="acme.com", g2_slug="acme-crm")
    assert acct.to_db_row()["g2_slug"] == "acme-crm"


def test_account_from_db_row_reads_g2_slug():
    acct = Account.from_db_row({"domain": "acme.com", "g2_slug": "acme-crm"})
    assert acct.g2_slug == "acme-crm"
