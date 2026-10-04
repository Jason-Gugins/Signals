# src/sources/xray/strings.py
"""PURE X-ray query builder. Library spec + slots -> operator query string.

The five operators: site:, quoted phrases (exact match), OR (variants),
minus (noise), intitle:/inurl:. The human-readable string (not the URL
encoding) is the artifact recorded in the ledger so runs stay comparable.
"""
from __future__ import annotations

from typing import Iterable
from urllib.parse import quote_plus

def quote_phrase(phrase: str) -> str:
    p = " ".join((phrase or "").split()).strip('"')
    return f'"{p}"' if p else ""

def or_group(phrases: Iterable[str]) -> str:
    quoted = [q for q in (quote_phrase(p) for p in phrases) if q]
    if not quoted:
        return ""
    return quoted[0] if len(quoted) == 1 else "(" + " OR ".join(quoted) + ")"

def minus_group(terms: Iterable[str]) -> str:
    return " ".join("-" + quote_phrase(t) for t in terms if quote_phrase(t))

def build_query(spec: dict, **slots: str | None) -> str:
    """Fill {curly} slots in variants/phrases, emit operators in fixed order:
    site, intitle:, inurl:, OR-variants, quoted phrases, minus-group.
    A phrase with ANY unfilled slot drops out entirely — a slot passed as None
    or simply absent both count as unfilled (a literal "{niche}" never reaches
    a live query). Multi-slot phrases fill textually, so a HALF-filled
    multi-slot phrase also drops (it still carries a brace) — the library
    ships none, but the rule is enforced, not assumed."""
    def fill(text: str) -> str:
        for key, val in slots.items():
            text = text.replace("{" + key + "}", (val or "").strip())
        return text

    def filled(field: str) -> list[str]:
        # A phrase still carrying an unfilled {slot} after filling drops out
        # ENTIRELY — whether the slot was passed as None or never passed at
        # all (absent key == unfilled; a literal "{niche}" in a live query is
        # never acceptable).
        vals = []
        for t in (spec.get(field) or []):
            v = fill(t).strip()
            if v and "{" not in v:
                vals.append(v)
        return vals

    parts: list[str] = []
    if (spec.get("site") or "").strip():
        parts.append(f"site:{spec['site'].strip()}")
    if (spec.get("intitle") or "").strip():
        parts.append(f"intitle:{quote_phrase(fill(spec['intitle']))}")
    if (spec.get("inurl") or "").strip():
        parts.append(f"inurl:{fill(spec['inurl']).strip()}")
    variants = or_group(filled("variants"))
    if variants:
        parts.append(variants)
    parts.extend(quote_phrase(p) for p in filled("phrases"))
    minus = minus_group(filled("exclude"))
    if minus:
        parts.append(minus)
    return " ".join(parts)

def encode_query(query: str, engine: str) -> str:
    if engine == "google_state":
        # google_state path (bypass ladder Task 6): the replay-proven URL
        # shape — plain /search?q=...&hl=en, NO num/filter params (the
        # cookie replay that returned the 12-record W_jd body fetched
        # exactly this shape). Result-bearing fetches REQUIRE the fresh
        # browser-harvested cookie jar; cookies ride the FETCH layer
        # (cli._xray_google_state_fetch -> data/xray/google_cookies.json),
        # not the URL.
        return f"https://www.google.com/search?q={quote_plus(query)}&hl=en"
    if engine == "google":
        return f"https://www.google.com/search?q={quote_plus(query)}&num=20&hl=en&filter=0"
    if engine == "ddg_lite":
        # The ONLY probe-validated keyless path (data/probe/XRAY_SERP_2026_10.md):
        # lite.duckduckgo.com/lite/ GET — the html endpoint 403s operator
        # queries and google is NO-GO keylessly on every transport.
        return f"https://lite.duckduckgo.com/lite/?q={quote_plus(query)}"
    if engine == "ddg":
        return f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    raise ValueError(f"unknown engine: {engine}")
