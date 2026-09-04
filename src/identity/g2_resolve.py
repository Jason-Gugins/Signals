"""Auto-resolve G2 product slug from a company name via G2 search.

PURE parsing functions + a thin URL builder for the orchestrator.

Selection contract (mirrors ``src/identity/capterra_resolve.py`` — the house
"never guess on ambiguity" rule): exact case-insensitive name -> normalized
(punctuation/space insensitive) name -> unique substring match in either
direction -> otherwise NO slug, with the candidate results surfaced. A wrong
g2_slug burns anti-bot budget on another company's reviews, so ambiguity is
never resolved by falling back to the first search result.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote_plus


def parse_g2_search_results(html: str) -> list[dict]:
    """Parse G2 search results HTML into a list of {slug, name, url} dicts. PURE."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    results = []
    for link in soup.select("a[href*='/products/']"):
        href = link.get("href", "")
        m = re.search(r"/products/([^/]+)", href)
        if not m:
            continue
        slug = m.group(1)
        if slug in ("reviews", "compare", "categories"):
            continue
        name_el = link.select_one(".product-card__product-name") or link
        name = name_el.get_text(strip=True) if name_el else slug
        results.append({"slug": slug, "name": name, "url": href})
    seen = set()
    deduped = []
    for r in results:
        if r["slug"] not in seen:
            seen.add(r["slug"])
            deduped.append(r)
    return deduped


@dataclass
class G2ResolveResult:
    """Outcome of a G2 name → slug resolution (mirrors capterra_resolve.ResolveResult)."""

    status: str  # "resolved" | "ambiguous" | "no_match"
    slug: Optional[str] = None  # the G2 product slug when resolved
    candidates: list[dict] = field(default_factory=list)


def _norm(value: str) -> str:
    """Lowercase and strip everything except alphanumerics. PURE."""
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def resolve_g2_from_results(company_name: str, search_results: list[dict]) -> G2ResolveResult:
    """Pick the G2 product slug from search results for a company name. PURE.

    Ladder (mirrors capterra_resolve.resolve_capterra_from_results): 1) exact
    case-insensitive name, 2) normalized equality, 3) unique substring match
    in either direction, else ambiguous / no match with the candidates
    surfaced. Never guesses among multiple plausible matches.
    """
    if not search_results:
        return G2ResolveResult(status="no_match")
    target = company_name.lower().strip()
    target_norm = _norm(company_name)

    for r in search_results:
        if r["name"].lower().strip() == target:
            return G2ResolveResult(status="resolved", slug=r["slug"], candidates=[r])
    for r in search_results:
        if _norm(r["name"]) == target_norm:
            return G2ResolveResult(status="resolved", slug=r["slug"], candidates=[r])

    partial = [
        r
        for r in search_results
        if target_norm
        and (
            target_norm in _norm(r["name"])
            or (_norm(r["name"]) and _norm(r["name"]) in target_norm)
        )
    ]
    if len(partial) == 1:
        return G2ResolveResult(status="resolved", slug=partial[0]["slug"], candidates=[partial[0]])
    if len(partial) > 1:
        return G2ResolveResult(status="ambiguous", candidates=partial)
    return G2ResolveResult(status="no_match", candidates=search_results)


def resolve_g2_slug(company_name: str, search_results: list[dict]) -> Optional[str]:
    """Pick the best G2 product slug for a company name — candidates-only.

    Backward-compatible wrapper around :func:`resolve_g2_from_results`:
    returns the slug ONLY on an exact/unique match, None on empty results or
    ambiguity (never the first result). PURE.
    """
    return resolve_g2_from_results(company_name, search_results).slug


def fetch_g2_search_url(company_name: str) -> str:
    """Build the G2 search URL for a company name. PURE."""
    return f"https://www.g2.com/search?query={quote_plus(company_name)}"
