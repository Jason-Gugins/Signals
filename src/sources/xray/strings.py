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
    A phrase whose slot is unfilled drops out entirely. Multi-slot phrases
    fill textually — a HALF-filled multi-slot phrase passes through (e.g.
    "{title} in {location}" with location=None -> "head in"), so the library
    must not ship multi-slot phrases (it doesn't)."""
    def fill(text: str) -> str:
        for key, val in slots.items():
            text = text.replace("{" + key + "}", (val or "").strip())
        return text

    def filled(field: str) -> list[str]:
        return [v for v in (fill(t).strip() for t in (spec.get(field) or [])) if v]

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
    if engine == "google":
        return f"https://www.google.com/search?q={quote_plus(query)}&num=20&hl=en&filter=0"
    if engine == "ddg_lite":
        # The ONLY probe-validated path (data/probe/XRAY_SERP_2026_10.md):
        # lite.duckduckgo.com/lite/ GET — the html endpoint 403s operator
        # queries and google is a NO-GO on every transport.
        return f"https://lite.duckduckgo.com/lite/?q={quote_plus(query)}"
    if engine == "ddg":
        return f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    raise ValueError(f"unknown engine: {engine}")
