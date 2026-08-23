"""PURE Form D issuer vs homepage match. No I/O."""

from __future__ import annotations

from src.identity.names import name_similarity, normalize_name
from src.sources.sec.page_peel import PageHints
from src.sources.sec.parse_formd import FormD

JUNK = frozenset({
    "surgical", "safety", "fund", "reit", "holdings", "feeder", "partners",
    "lp", "l", "p", "series", "ventures", "village",
})
COMMON = frozenset({
    "parallel", "scanner", "apex", "atlas", "nova", "pulse",
    "clay", "momentum", "speak", "polymarket",
})


def issuer_matches_page(fd: FormD, hints: PageHints, *, brand: str) -> bool:
    ent = normalize_name(fd.entity_name) or ""
    brand_n = normalize_name(brand) or ""
    if not ent or not brand_n:
        return False
    peeled = list(hints.legal_names) + list(hints.site_names) + list(hints.titles)
    if _strong_peel_hit(fd.entity_name, peeled, brand_n):
        return True
    bt, et = set(brand_n.split()), set(ent.split())
    extra = et - bt
    blob_tokens = set((hints.text_blob or "").casefold().split())
    if extra & JUNK and not (extra & JUNK & blob_tokens):
        return False
    if not bt <= et:
        return False
    if brand_n in COMMON and not (extra & blob_tokens):
        return False
    return True


def _strong_peel_hit(entity: str, peeled: list[str], brand_n: str) -> bool:
    for p in peeled:
        if not p:
            continue
        if name_similarity(entity, p) < 0.72:
            continue
        pn = normalize_name(p) or ""
        if brand_n in COMMON and pn == brand_n:
            continue
        return True
    return False
