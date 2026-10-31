"""Characterization pin: the two parallel name-normalization stacks DIVERGE.

WHY two stacks exist
--------------------
``src/identity/names.py`` (``normalize_name``) serves EDGAR/CIK matching and
domain matching: its output feeds ``name_similarity`` (match_cik's fuzzy arm),
``name_tokens``, and ``name_matches_domain`` (the waterfall + DDG scorer). Its
``LEGAL_SUFFIXES`` set is broad — sa, srl, bv, ab, oy, plc, pty, lp, llp,
pllc, ag, nv, spa, pte — because EDGAR titles arrive in international legal
forms. ``src/identity/resolve.py`` (``normalize_entity``) serves account
identity keys: registry name/entity aliases and queue lookups. Its suffix set
is deliberately small and unambiguous — inc, llc, ltd, corp, co, gmbh,
incorporated, limited, corporation, company.

Same input, different key depending on the code path. Every expected value in
this file was computed by running both functions at authoring time (pure
characterization — passes by construction), so the divergence is visible and
pinned instead of latent. If either stack changes, this file goes red and the
change must be made consciously.

UNIFYING the stacks is a recorded PRODUCT DECISION, not this test's job:
see docs/plans/2026-10-30-name-resolution-tests.md (Task 6) and the matching
TODO.md deferred entry. This test only pins the current split.

Known observable divergences pinned below:
  * Suffix sets differ in BOTH directions: 'co'/'company' (plus inc/llc/ltd/
    limited/corp/corporation/gmbh) are shared; 'sa'/'bv'/'plc' (plus srl/ab/
    oy/pty/lp/llp/pllc/ag/nv/spa/pte) are names-only; 'incorporated' is
    resolve-only.
  * "Acme SPA" / "Acme Pte" / "Acme PLC" -> names strips to "acme", resolve
    keeps the suffix.
  * "Acme Incorporated" -> resolve strips to "acme", names keeps the word.
  * Dotted forms "Acme S.A." / "Acme B.V." do NOT diverge: punctuation is
    rewritten to spaces BEFORE suffix stripping, fragmenting "sa"/"bv" into
    "s a"/"b v" in BOTH stacks (the intuitive guess that names strips them
    is wrong).
  * names re-drops a leading "the" after suffix stripping ("The The Company"
    -> None); resolve drops it once ("the").
  * Empty contract: names returns None for blank/fully-stripped input,
    resolve returns "".
"""

from __future__ import annotations

import pytest

from src.identity.names import LEGAL_SUFFIXES as NAMES_SUFFIXES
from src.identity.names import normalize_name
from src.identity.resolve import LEGAL_SUFFIXES as RESOLVE_SUFFIXES
from src.identity.resolve import normalize_entity

# (input, names.normalize_name, resolve.normalize_entity)
# Computed 2026-10-30 against HEAD. Rows whose id ends in DIVERGE are the
# point of this file: one input, two different keys depending on code path.
CHARACTERIZATION_TABLE = [
    # "S.A." does NOT diverge: dots -> spaces before suffix stripping
    # fragments "sa" into "s a"; neither stack's suffix set sees it.
    pytest.param("Acme S.A.", "acme s a", "acme s a", id="dotted-sa-fragments-in-both"),
    # DIVERGE: "spa" is a names-only suffix -> "acme"; resolve keeps it.
    pytest.param("Acme SPA", "acme", "acme spa", id="spa-names-only-suffix-DIVERGES"),
    # "B.V." same story as "S.A.": "bv" fragments to "b v" in both stacks.
    pytest.param("Acme B.V.", "acme b v", "acme b v", id="dotted-bv-fragments-in-both"),
    # DIVERGE: "pte" is a names-only suffix -> "acme"; resolve keeps it
    # ("Acme Pte" is a DIFFERENT key on the alias/queue side).
    pytest.param("Acme Pte", "acme", "acme pte", id="pte-names-only-suffix-DIVERGES"),
    # "ltd" then "co" are repeatedly stripped by BOTH suffix sets -> agree.
    pytest.param("Acme Co Ltd", "acme", "acme", id="co-ltd-stripped-by-both"),
    pytest.param("Acme Two", "acme two", "acme two", id="plain-name-agrees"),
    pytest.param("Acme 2", "acme 2", "acme 2", id="numeric-token-kept-by-both"),
    # Both drop the leading article once and strip "corporation" -> agree.
    pytest.param("the Acme Corporation", "acme", "acme", id="leading-the-plus-corporation"),
    pytest.param("Acme Corporation", "acme", "acme", id="corporation-stripped-by-both"),
]


@pytest.mark.parametrize(("name", "names_output", "resolve_output"), CHARACTERIZATION_TABLE)
def test_normalization_table(name: str, names_output: str, resolve_output: str) -> None:
    assert normalize_name(name) == names_output
    assert normalize_entity(name) == resolve_output


# Further name-input divergences computed at authoring time.
SUPPLEMENTARY_TABLE = [
    # DIVERGE: "incorporated" is resolve-only — names keeps it as a word.
    pytest.param(
        "Acme Incorporated",
        "acme incorporated",
        "acme",
        id="incorporated-resolve-only-suffix-DIVERGES",
    ),
    # DIVERGE: dot-free "PLC" IS stripped by names, kept by resolve (contrast
    # the dotted rows above, where both stacks keep it).
    pytest.param("Acme PLC", "acme", "acme plc", id="plc-names-only-suffix-DIVERGES"),
    # Agreement: both stacks drop exactly the leading article here.
    pytest.param("The Acme", "acme", "acme", id="leading-the-dropped-by-both"),
    # DIVERGE: names re-drops "the" AFTER suffix stripping ("company" goes,
    # the remaining leading "the" goes too -> None); resolve drops "the" only
    # once, before suffix stripping -> "the".
    pytest.param("The The Company", None, "the", id="double-the-drop-DIVERGES"),
]


@pytest.mark.parametrize(("name", "names_output", "resolve_output"), SUPPLEMENTARY_TABLE)
def test_supplementary_rows(name: str, names_output: str | None, resolve_output: str) -> None:
    assert normalize_name(name) == names_output
    assert normalize_entity(name) == resolve_output


def test_empty_and_fully_stripped_contract_diverges() -> None:
    """names returns None for blank/fully-stripped input; resolve returns ''.

    Both are falsy but they are different sentinels — callers must not
    interchange results across stacks.
    """
    for blank in (None, "", "   ", "Co Ltd"):
        assert normalize_name(blank) is None
        assert normalize_entity(blank) == ""


def test_suffix_set_shared_tokens() -> None:
    """'co'/'company' — and the rest of the core Anglo set — are in BOTH
    stacks' LEGAL_SUFFIXES; this exact-intersection pin is what makes any
    future set edit on either side fail loudly here."""
    assert NAMES_SUFFIXES & RESOLVE_SUFFIXES == {
        "inc",
        "llc",
        "ltd",
        "limited",
        "corp",
        "corporation",
        "gmbh",
        "co",
        "company",
    }


def test_suffix_set_names_only_tokens() -> None:
    """'sa'/'bv'/'plc' (and srl/ab/oy/pty/lp/llp/pllc/ag/nv/spa/pte) exist
    ONLY in names.LEGAL_SUFFIXES — resolve never strips them."""
    assert NAMES_SUFFIXES - RESOLVE_SUFFIXES == {
        "sa",
        "srl",
        "bv",
        "ab",
        "oy",
        "plc",
        "pty",
        "lp",
        "llp",
        "pllc",
        "ag",
        "nv",
        "spa",
        "pte",
    }


def test_suffix_set_resolve_only_token() -> None:
    """'incorporated' exists ONLY in resolve.LEGAL_SUFFIXES — names keeps it
    as an ordinary word (see the 'Acme Incorporated' row above)."""
    assert RESOLVE_SUFFIXES - NAMES_SUFFIXES == {"incorporated"}
