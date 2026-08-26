"""Auto-resolve G2 product slug from a company name via G2 search.

PURE parsing functions + a thin URL builder for the orchestrator.
"""
from __future__ import annotations

import re
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


def resolve_g2_slug(company_name: str, search_results: list[dict]) -> Optional[str]:
    """Pick the best G2 product slug from search results for a company name.

    Strategy: 1) exact case-insensitive name match, 2) substring match, 3) first result.
    Returns None if search_results is empty. PURE.
    """
    if not search_results:
        return None
    target = company_name.lower().strip()
    for r in search_results:
        if r["name"].lower().strip() == target:
            return r["slug"]
    for r in search_results:
        name = r["name"].lower().strip()
        if target in name or name in target:
            return r["slug"]
    return search_results[0]["slug"]


def fetch_g2_search_url(company_name: str) -> str:
    """Build the G2 search URL for a company name. PURE."""
    return f"https://www.g2.com/search?query={quote_plus(company_name)}"
