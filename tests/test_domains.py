"""Tests for registrable-domain normalization."""

from __future__ import annotations

from src.identity.domains import (
    HOSTING_DOMAINS,
    MULTI_LABEL_TLDS,
    PUBLIC_EMAIL_DOMAINS,
    domain_variants,
    is_public_email_domain,
    root_domain,
    same_org,
)


def test_root_domain_url_strips_scheme_www_path_query():
    assert root_domain("https://WWW.Gong.io/pricing?x=1") == "gong.io"


def test_root_domain_email_multi_label_tld():
    assert root_domain("jane@sub.acme.co.uk") == "acme.co.uk"
    assert "co.uk" in MULTI_LABEL_TLDS


def test_root_domain_rejects_public_email_localhost_ip_empty():
    assert root_domain("user@gmail.com") is None
    assert root_domain("http://localhost:3000") is None
    assert root_domain("localhost") is None
    assert root_domain("192.168.1.1") is None
    assert root_domain("http://10.0.0.8/x") is None
    assert root_domain("") is None
    assert root_domain(None) is None
    assert root_domain("   ") is None


def test_root_domain_trailing_dot_and_uppercase():
    assert root_domain("WWW.ACME.COM.") == "acme.com"
    assert root_domain("Acme.COM") == "acme.com"


def test_root_domain_unicode_passthrough():
    assert root_domain("https://münchen.example/path") == "münchen.example"
    assert root_domain("https://例え.jp") == "例え.jp"


def test_root_domain_strips_port_and_path():
    assert root_domain("https://jobs.acme.com:443/careers") == "acme.com"
    assert root_domain("acme.com/about") == "acme.com"


def test_root_domain_multi_label_variants():
    assert root_domain("shop.example.com.au") == "example.com.au"
    assert root_domain("www.foo.co.nz") == "foo.co.nz"
    assert root_domain("a.b.co.jp") == "b.co.jp"
    assert root_domain("x.com.br") == "x.com.br"
    assert root_domain("y.co.in") == "y.co.in"
    assert root_domain("z.com.mx") == "z.com.mx"


def test_public_email_and_hosting_sets():
    for d in ("gmail.com", "outlook.com", "hotmail.com", "yahoo.com", "icloud.com", "proton.me"):
        assert d in PUBLIC_EMAIL_DOMAINS
        assert is_public_email_domain(d)
    assert is_public_email_domain("GMAIL.COM")
    assert not is_public_email_domain("acme.com")
    for d in ("wixsite.com", "squarespace.com", "github.io", "herokuapp.com"):
        assert d in HOSTING_DOMAINS


def test_same_org():
    assert same_org("gong.io", "www.gong.io") is True
    assert same_org("www.gong.io", "gong.io") is True
    assert same_org("app.gong.io", "gong.io") is True
    assert same_org("gong.io", "gongcha.com") is False
    assert same_org("gong.io", None) is False
    assert same_org(None, "gong.io") is False
    assert same_org("gong.io", "gong.io") is True


def test_domain_variants_stable():
    assert domain_variants("gong.io") == ["gong.io", "www.gong.io", "get.gong.io"]
    assert domain_variants("WWW.Gong.io") == ["gong.io", "www.gong.io", "get.gong.io"]
