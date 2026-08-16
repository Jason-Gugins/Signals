"""Tests for company-name normalization and matching."""

from __future__ import annotations

from src.identity.names import (
    LEGAL_SUFFIXES,
    name_matches_domain,
    name_similarity,
    name_tokens,
    normalize_name,
)


def test_normalize_name_strips_the_suffix_punct():
    assert normalize_name("The Acme Corporation, Inc.") == "acme"
    assert normalize_name("ACME LLC") == "acme"
    assert normalize_name("Gong.io") == "gong io"
    assert normalize_name("") is None
    assert normalize_name(None) is None
    assert normalize_name("   ") is None


def test_legal_suffixes_present():
    for s in ("inc", "llc", "ltd", "limited", "corp", "corporation", "gmbh", "sa", "srl", "bv", "ab", "oy", "plc", "pty", "co"):
        assert s in LEGAL_SUFFIXES


def test_name_tokens():
    assert name_tokens("The Acme Corporation, Inc.") == ["acme"]
    assert "sales" in name_tokens("Acme Sales Ltd")


def test_name_similarity_symmetric_and_identity():
    a, b = "Gong Inc.", "Gong"
    assert name_similarity(a, a) == 1.0
    assert name_similarity(a, b) == name_similarity(b, a)
    assert name_similarity("Acme", "Acme") == 1.0
    assert name_similarity("Acme", "Completely Different Co") < 0.5
    assert name_similarity("Gong", "Gong Labs") >= 0.5


def test_name_matches_domain():
    assert name_matches_domain("Gong.io", "gong.io") is True
    assert name_matches_domain("Acme Corp", "acmecorp.com") is True
    assert name_matches_domain("Acme", "zendesk.com") is False
    assert name_matches_domain("Acme Corporation", "acme.com") is True
    assert name_matches_domain("", "acme.com") is False
