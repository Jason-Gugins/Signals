"""Extract explicit operational-need statements from stored documents.

A "need" here is a company saying, in its own published words, that it is
building / rolling out / standing up / migrating / consolidating / expanding
into something, or hiring / looking for something. This module is deliberately
literal: it does substring matching against a fixed phrase list. There is no
stemming, no fuzzy matching, and no LLM. The matched sentence is returned
verbatim as the quote, so the quote *is* the evidence -- never a paraphrase.

Pure and deterministic: no I/O, no clock, no caching.
"""

from __future__ import annotations

import re

#: Need phrases. Each starts with a first-person company subject.
NEED_PHRASES: tuple[str, ...] = (
    "we are building",
    "we're building",
    "we are rolling out",
    "we're rolling out",
    "we are standing up",
    "we're standing up",
    "we are migrating",
    "we're migrating",
    "we are consolidating",
    "we're consolidating",
    "we are expanding into",
    "we're expanding into",
    "we are hiring for",
    "we're hiring for",
    "we are looking for",
    "we're looking for",
)

#: Longest phrases first so an alternation cannot match a shorter prefix.
_NEED_PATTERN = re.compile(
    "|".join(re.escape(p) for p in sorted(NEED_PHRASES, key=len, reverse=True))
)

#: Sentences are split on whitespace that follows a . ! or ? terminator.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

_CURLY_APOSTROPHE = "\u2019"

MIN_WORDS = 4
MAX_CHARS = 400


def _normalise(sentence: str) -> str:
    """Casefold and straighten apostrophes -- for matching only."""
    return sentence.casefold().replace(_CURLY_APOSTROPHE, "'")


def extract_need_statements(text: str) -> list[dict]:
    """Return [{"quote", "matched_phrase"}, ...] in document order.

    ``quote`` is the original sentence text, stripped, exactly as it appeared
    in ``text``. ``matched_phrase`` comes from the normalised copy, so it is
    lowercase with straight apostrophes.
    """
    if not text or not text.strip():
        return []

    rows: list[dict] = []
    seen: set[str] = set()

    for piece in _SENTENCE_SPLIT.split(text):
        quote = piece.strip()
        if not quote:
            continue
        if len(quote) > MAX_CHARS:
            continue
        if len(quote.split()) < MIN_WORDS:
            continue

        match = _NEED_PATTERN.search(_normalise(quote))
        if match is None:
            continue

        if quote in seen:
            continue
        seen.add(quote)
        rows.append({"quote": quote, "matched_phrase": match.group(0)})

    return rows