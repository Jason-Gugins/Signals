# src/sources/xray/hits.py
"""PURE hit extraction from parsed SERP results. Never-guess: title/company
parsed from SERP text are best-effort (confidence 0.5) and every hit carries
its source URL; nothing here decides outreach — downstream review does.

Input is the ``[{"url", "title", "snippet"}]`` list that
``src/sources/xray/serp.py:parse_results`` returns. Two extractions:

- ``extract_profiles`` — LinkedIn ``/in/`` slugs, deduped by lowercase slug;
  title/company are best-effort parses of the SERP title ("Title at Company"
  / "Title @ Company"); ``name`` stays empty (SERP text is evidence, not a
  verified identity — the downstream contact layer fills verified fields).
- ``extract_companies`` — non-profile hosts normalized to registrable root
  domains via ``src/identity/domains.py:root_domain`` (imported, never
  reimplemented). A hit is dropped when its ROOT domain is a known cohort
  account (already harvested — subdomain hits of known roots fall out of the
  same root check), is excluded, or was already seen (first hit wins).

Purity: no I/O, no clock, no network/sqlite3 imports — the runner task owns
fetch, pacing and persistence.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from src.identity.domains import root_domain

LINKEDIN_PROFILE_RE = re.compile(
    r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/([A-Za-z0-9_%\-]{3,100})", re.I)
TITLE_AT_RE = re.compile(r"^(.{2,80}?)\s+(?:at|@)\s+(.{2,80})$")


@dataclass(frozen=True)
class ProfileHit:
    slug: str
    url: str
    name: str
    title: str
    company: str
    string_id: str
    serp_title: str
    snippet: str


@dataclass(frozen=True)
class CompanyHit:
    domain: str
    url: str
    string_id: str
    serp_title: str
    snippet: str


def extract_profiles(results: list[dict], *, string_id: str) -> list[ProfileHit]:
    """LinkedIn profile hits, deduped by lowercase slug, in document order."""
    hits: list[ProfileHit] = []
    seen: set[str] = set()
    for r in results:
        m = LINKEDIN_PROFILE_RE.search(r["url"])
        if not m:
            continue
        slug = m.group(1)
        if not slug or slug.lower() in seen:
            continue
        seen.add(slug.lower())
        title, company = "", ""
        tm = TITLE_AT_RE.match(r["title"] or "")
        if tm:
            title, company = tm.group(1).strip(), tm.group(2).strip()
        hits.append(ProfileHit(
            slug=slug,
            url=r["url"],
            name="",
            title=title,
            company=company,
            string_id=string_id,
            serp_title=r["title"],
            snippet=r["snippet"],
        ))
    return hits


def extract_companies(
    results: list[dict],
    *,
    string_id: str,
    known_domains: set[str],
    exclude: set[str],
) -> list[CompanyHit]:
    """Company-page hits, root-domain normalized, cohort-known roots dropped.

    ``domain`` carries the hit's own host (leading ``www.`` stripped); the
    drop/dedupe decisions run on the registrable root from ``root_domain``.
    """
    known = {d.casefold().removeprefix("www.") for d in (known_domains or set())}
    banned = {d.casefold().removeprefix("www.") for d in (exclude or set())}
    hits: list[CompanyHit] = []
    seen_roots: set[str] = set()
    for r in results:
        if LINKEDIN_PROFILE_RE.search(r["url"]):
            continue
        host = urlsplit_host(r["url"])
        if not host:
            continue
        root = root_domain(host)
        if not root:
            continue
        if root in known or root in banned or root in seen_roots:
            continue
        # exclude entries that are themselves subdomains (e.g. blog.acme.com)
        # also kill their own sub-hits.
        if any(host == e or host.endswith("." + e) for e in banned):
            continue
        seen_roots.add(root)
        hits.append(CompanyHit(
            domain=host,
            url=r["url"],
            string_id=string_id,
            serp_title=r["title"],
            snippet=r["snippet"],
        ))
    return hits


def urlsplit_host(url: str) -> str:
    """Lowercase netloc with a leading ``www.`` stripped; "" on bad input."""
    try:
        netloc = (urlsplit(url).netloc or "").lower()
    except ValueError:
        return ""
    return netloc.removeprefix("www.")
