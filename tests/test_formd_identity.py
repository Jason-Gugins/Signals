"""Tests for Form D issuer → registry attach."""

from __future__ import annotations

from pathlib import Path

from src.core.db import Database
from src.core.models import Account
from src.identity.domains import root_domain
from src.identity.registry import AccountRegistry
from src.sources.sec.formd_identity import attach_form_d_account, stub_domain
from src.sources.sec.parse_formd import parse_form_d


FIX = Path(__file__).parent / "fixtures" / "sec" / "form_d_primary_doc.xml"


def _reg(tmp_path) -> AccountRegistry:
    return AccountRegistry(Database(tmp_path / "s.db"))


def _fd():
    return parse_form_d(FIX.read_bytes())


def test_stub_domain_survives_root_domain():
    assert stub_domain("1234567") == "cik0001234567.edgar"
    assert "." in stub_domain("0001234567")
    assert root_domain(stub_domain("0001234567")) == stub_domain("0001234567")


def test_existing_cik_alias_reused(tmp_path):
    reg = _reg(tmp_path)
    reg.upsert(Account(domain="acme.com", name="Acme", cik="0001234567"))
    got = attach_form_d_account(_fd(), reg)
    assert got.domain == "acme.com"
    rows = reg.db.query("SELECT domain FROM accounts")
    assert [r["domain"] for r in rows] == ["acme.com"]


def test_existing_name_gets_cik(tmp_path):
    reg = _reg(tmp_path)
    reg.upsert(Account(domain="acme.com", name="Acme Robotics Inc."))
    got = attach_form_d_account(_fd(), reg)
    assert got.domain == "acme.com"
    assert got.cik == "0001234567"
    rows = reg.db.query("SELECT domain FROM accounts")
    assert [r["domain"] for r in rows] == ["acme.com"]


def test_unknown_issuer_stubs_cik_domain(tmp_path):
    reg = _reg(tmp_path)
    got = attach_form_d_account(_fd(), reg, cohort="formd")
    assert got.domain == "cik0001234567.edgar"
    assert got.cik == "0001234567"
    assert got.seed_source == "sec_formd"
    assert got.name == "Acme Robotics Inc."
    assert got.cohort == "formd"
    assert reg.get("cik0001234567.edgar") is not None
    assert reg.resolve(cik="0001234567").domain == "cik0001234567.edgar"


def test_prefer_domain_empty_registry(tmp_path):
    reg = _reg(tmp_path)
    got = attach_form_d_account(_fd(), reg, prefer_domain="radicl.com")
    assert got.domain == "radicl.com"
    assert got.cik == "0001234567"
    assert [r["domain"] for r in reg.db.query("SELECT domain FROM accounts")] == ["radicl.com"]


def test_prefer_domain_remaps_cik_stub(tmp_path):
    reg = _reg(tmp_path)
    stub = attach_form_d_account(_fd(), reg)
    assert stub.domain == "cik0001234567.edgar"
    got = attach_form_d_account(_fd(), reg, prefer_domain="radicl.com")
    assert got.domain == "radicl.com"
    domains = [r["domain"] for r in reg.db.query("SELECT domain FROM accounts")]
    assert "radicl.com" in domains
    assert "cik0001234567.edgar" not in domains


def test_prefer_domain_does_not_steal_real_cik(tmp_path):
    reg = _reg(tmp_path)
    reg.upsert(Account(domain="acme.com", name="Acme", cik="0001234567"))
    got = attach_form_d_account(_fd(), reg, prefer_domain="radicl.com")
    assert got.domain == "acme.com"
    assert [r["domain"] for r in reg.db.query("SELECT domain FROM accounts")] == ["acme.com"]
