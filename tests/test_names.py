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


class TestKnownLimitations:
    """Characterization pins for name_matches_domain's loose boundary.

    Plan: docs/plans/2026-10-30-name-resolution-tests.md (Task 4).

    WHY these are pinned: name_matches_domain (src/identity/names.py:101-119)
    is the shared "same org" scorer for ALL identity waterfall stages plus the
    DDG strict rule. Its first-token rule (tokens[0] == label) and its prefix
    rule (label.startswith(compact) or compact.startswith(label), guarded only
    by len >= 3) form a documented false-positive class for generic brand
    tokens: a bare token like "Signal" or "Delta" matches any domain that
    shares it as a prefix, regardless of the actual company. This behavior is
    deliberately left UNCHANGED this wave — changing the boundary policy is a
    recorded product decision, not a test-wave fix. These tests pin what the
    code actually does today so any future change is a conscious, visible one.
    """

    def test_prefix_rule_false_positive_signal_vs_signalfire(self):
        # FALSE POSITIVE, pinned: "Signal" (compact "signal") is a prefix of
        # the label "signalfire", and both sides clear the len>=3 guard, so
        # the prefix rule (names.py:117-118) declares a match even though
        # SignalFire is a different company. The generic brand token is
        # trusted as a sufficient brand signal.
        assert name_matches_domain("Signal", "signalfire.com") is True

    def test_generic_first_token_false_positive_delta_vs_deltafaucet(self):
        # FALSE POSITIVE, pinned: the plan attributes this class to the
        # first-token rule (names.py:115), but mechanically the prefix arm
        # fires for this exact pair — compact "delta" startswith-matches
        # label "deltafaucet" (names.py:117-118). Same false-positive class,
        # same root cause: a bare generic token ("Delta") matches any domain
        # sharing it as a prefix, so Delta Faucet would be accepted as a
        # match for an account named just "Delta".
        assert name_matches_domain("Delta", "deltafaucet.com") is True

    def test_must_stay_true_whole_token_equality(self):
        # Control for the false-positive pins: compact-token equality
        # (names.py:113) is the intended match path and must stay True.
        assert name_matches_domain("Acme Corp", "acme.com") is True

    def test_must_stay_true_hyphen_collapse(self):
        # Hyphenated domains collapse via label.replace("-", "") before the
        # comparison; "Northwind Data" -> compact "northwinddata" equals the
        # collapsed label of "northwind-data.com".
        assert name_matches_domain("Northwind Data", "northwind-data.com") is True

    def test_must_stay_true_first_token_rule_positive(self):
        # The first-token rule earning its keep: "Signal.ai" tokenizes to
        # ["signal", "ai"], so compact "signalai" != label "signal", but
        # tokens[0] == "signal" == label (names.py:115) -> True.
        assert name_matches_domain("Signal.ai", "signal.ai") is True

    def test_must_stay_true_three_char_brand_guard_boundary(self):
        # "Iox" is exactly 3 chars: the len>=3 guard is the only thing keeping
        # short brand tokens in play. Equality fires here (compact == label),
        # but one char shorter and the prefix rule would refuse; pin the
        # boundary as it stands.
        assert name_matches_domain("Iox", "iox.com") is True

    def test_both_false_negative_acme_vs_widgetworks(self):
        # Conservative direction, pinned: no rule fires — no equality, first
        # token "acme" != label "widgetworks", no prefix relation either way.
        assert name_matches_domain("Acme", "widgetworks.com") is False
