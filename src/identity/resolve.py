"""Entity-name normalization and fuzzy matching for account identity.

Pure functions only — no I/O. Stdlib only (re, difflib).

`normalize_entity` canonicalizes company/legal names so that surface
variants ("Acme, Inc.", "acme inc", "The ACME corp.") compare equal.
`fuzzy_match` wraps difflib.SequenceMatcher over the *normalized* forms
with a conservative default threshold (0.87).
"""

from __future__ import annotations

import difflib
import re

# Legal-suffix tokens stripped from the tail of a normalized name.
# Kept deliberately small and unambiguous: tokens like "co" are only
# removed when they terminate the name (see _strip_suffixes).
LEGAL_SUFFIXES: frozenset[str] = frozenset(
    {"inc", "llc", "ltd", "corp", "co", "gmbh", "incorporated", "limited", "corporation", "company"}
)

_PUNCT = re.compile(r"[^\w\s]+", re.UNICODE)


def normalize_entity(name: str | None) -> str:
    """Canonical form of a company name for identity comparison.

    Lowercase (casefold), drop punctuation, drop leading "the", strip
    trailing legal suffixes, collapse whitespace. Returns "" for empty or
    fully-stripped input.
    """
    if not name:
        return ""
    text = name.casefold().strip()
    text = _PUNCT.sub(" ", text)
    tokens = text.split()
    if tokens and tokens[0] == "the":
        tokens = tokens[1:]
    tokens = _strip_suffixes(tokens)
    return " ".join(tokens)


def _strip_suffixes(tokens: list[str]) -> list[str]:
    """Remove trailing legal-suffix tokens (repeatedly, e.g. 'co ltd')."""
    while tokens and tokens[-1] in LEGAL_SUFFIXES:
        tokens = tokens[:-1]
    return tokens


def fuzzy_match(a: str, b: str, *, threshold: float = 0.87) -> bool:
    """True when normalized names match with ratio >= threshold.

    Exact normalized equality short-circuits to True. Uses
    difflib.SequenceMatcher on the normalized strings — stdlib only.
    """
    na, nb = normalize_entity(a), normalize_entity(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= threshold
