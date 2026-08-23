"""PURE homepage peel — legal-name hints from HTML bytes. No I/O."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html import unescape

from src.identity.domains import root_domain
from src.identity.names import normalize_name

DENY = frozenset({"webflow", "fonticons", "hubspot", "wordpress"})
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_OG_SITE = re.compile(
    r'<meta\b[^>]*(?:property|name)=["\']og:site_name["\'][^>]*>',
    re.I,
)
_META_DESC = re.compile(
    r'<meta\b[^>]*name=["\']description["\'][^>]*>',
    re.I,
)
_CONTENT = re.compile(r'\bcontent=["\']([^"\']+)["\']', re.I)
_LD = re.compile(
    r'<script\b[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.I | re.S,
)
_COPY = re.compile(r"(?:©|&copy;)\s+\d{4}\s+([^.<]{3,80})", re.I)
_COPYRIGHT = re.compile(r"Copyright\s+\d{4}\s+([^.<]{3,80})", re.I)
_LEGAL_NAME_Q = re.compile(r'"legalName"\s*:\s*"([^"]+)"')
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class PageHints:
    titles: tuple[str, ...]
    legal_names: tuple[str, ...]
    site_names: tuple[str, ...]
    cities: tuple[str, ...]
    text_blob: str


def _clean(s: str | None) -> str | None:
    if s is None:
        return None
    text = _WS.sub(" ", unescape(s)).strip()
    if text.startswith("|"):
        text = text.lstrip("| ").strip()
    return text or None


def _denied(s: str) -> bool:
    folded = s.casefold()
    tokens = set(re.split(r"[^\w]+", folded))
    return bool(tokens & DENY)


def _brand_segment(title: str) -> str | None:
    for sep in ("|", " - ", " – ", " — "):
        if sep in title:
            left = title.split(sep, 1)[0].strip()
            if 2 <= len(left) <= 40:
                return left
    if 2 <= len(title) <= 40:
        return title
    return None


def _domain_label(domain: str | None) -> str | None:
    if not domain:
        return None
    root = root_domain(domain) or domain
    label = root.split(".")[0].strip()
    return label.title() if label else None


def _parse_ld(raw: str) -> tuple[list[str], list[str], list[str]]:
    legal: list[str] = []
    names: list[str] = []
    cities: list[str] = []
    text = raw.strip()
    text = text.replace("https://***@type", 'https://schema.org","@type')
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        for m in _LEGAL_NAME_Q.finditer(raw):
            cleaned = _clean(m.group(1))
            if cleaned and not _denied(cleaned):
                legal.append(cleaned)
        return legal, names, cities

    nodes: list[object] = []
    if isinstance(data, list):
        nodes.extend(data)
    elif isinstance(data, dict):
        graph = data.get("@graph")
        if isinstance(graph, list):
            nodes.extend(graph)
        nodes.append(data)

    for node in nodes:
        if not isinstance(node, dict):
            continue
        typ = node.get("@type")
        types = {typ} if isinstance(typ, str) else set(typ or [])
        if types & {"FAQPage", "Question", "Answer", "BreadcrumbList", "WebPage", "SearchAction"}:
            continue
        ln = node.get("legalName")
        if isinstance(ln, str):
            cleaned = _clean(ln)
            if cleaned and not _denied(cleaned):
                legal.append(cleaned)
        nm = node.get("name")
        if isinstance(nm, str):
            cleaned = _clean(nm)
            if cleaned and not _denied(cleaned):
                names.append(cleaned)
        addr = node.get("address")
        if isinstance(addr, dict):
            city = addr.get("addressLocality")
            if isinstance(city, str):
                cleaned = _clean(city)
                if cleaned:
                    cities.append(cleaned)
    return legal, names, cities


def peel_page(html: bytes, *, domain: str | None = None, hint_name: str | None = None) -> PageHints:
    del hint_name
    text = html.decode("utf-8", errors="replace")
    titles: list[str] = []
    legal: list[str] = []
    sites: list[str] = []
    cities: list[str] = []
    blob_bits: list[str] = []

    for raw in _TITLE.findall(text):
        title = _clean(raw)
        if title:
            titles.append(title)
            blob_bits.append(title)
            brand = _brand_segment(title)
            if brand and brand != title:
                titles.append(brand)

    for tag in _OG_SITE.findall(text):
        m = _CONTENT.search(tag)
        name = _clean(m.group(1)) if m else None
        if name and not _denied(name):
            sites.append(name)

    for tag in _META_DESC.findall(text):
        m = _CONTENT.search(tag)
        desc = _clean(m.group(1)) if m else None
        if desc:
            blob_bits.append(desc)

    for block in _LD.findall(text):
        more_legal, more_names, more_cities = _parse_ld(block)
        legal.extend(more_legal)
        sites.extend(more_names)
        cities.extend(more_cities)

    for rx in (_COPY, _COPYRIGHT):
        for raw in rx.findall(text):
            name = _clean(raw)
            if not name:
                continue
            blob_bits.append(name)
            if _denied(name):
                continue
            legal.append(name)

    label = _domain_label(domain)
    if label:
        sites.append(label)

    blob = _WS.sub(" ", " ".join(blob_bits)).strip()[:4000]
    return PageHints(
        titles=tuple(dict.fromkeys(titles)),
        legal_names=tuple(dict.fromkeys(legal)),
        site_names=tuple(dict.fromkeys(sites)),
        cities=tuple(dict.fromkeys(cities)),
        text_blob=blob,
    )


def peel_legal_names(
    hints: PageHints, *, hint_name: str | None = None, domain: str | None = None
) -> list[str]:
    ordered: list[str] = []
    for item in hints.legal_names:
        if item and not _denied(item):
            ordered.append(item)
    for item in hints.titles:
        brand = _brand_segment(item) or item
        if brand and not _denied(brand):
            ordered.append(brand)
    for item in hints.site_names:
        if item and not _denied(item):
            ordered.append(item)
    if hint_name:
        cleaned = _clean(hint_name)
        if cleaned and not _denied(cleaned):
            ordered.append(cleaned)
    label = _domain_label(domain)
    if label:
        ordered.append(label)

    seen: set[str] = set()
    out: list[str] = []
    for name in ordered:
        key = normalize_name(name) or name.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
        if len(out) == 3:
            break
    return out
