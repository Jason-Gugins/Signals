"""PURE Form D issuer vs homepage match. No I/O."""

from __future__ import annotations

from src.identity.names import name_similarity, normalize_name
from src.sources.sec.page_peel import PageHints
from src.sources.sec.parse_formd import FormD

JUNK = frozenset({"surgical", "safety", "fund", "reit", "holdings", "feeder", "partners", "lp", "l", "p"})
COMMON = frozenset({"parallel", "scanner", "apex", "atlas", "nova", "pulse"})


def issuer_matches_page(fd: FormD, hints: PageHints, *, brand: str) -> bool:
    ent = normalize_name(fd.entity_name) or ""
    brand_n = normalize_name(brand) or ""
    if not ent or not brand_n:
        return False
    peeled = list(hints.legal_names) + list(hints.site_names) + list(hints.titles)
    if any(name_similarity(fd.entity_name, p) >= 0.72 for p in peeled if p):
        return True
    bt, et = set(brand_n.split()), set(ent.split())
    extra = et - bt
    blob_tokens = set((hints.text_blob or "").casefold().split())
    if extra & JUNK and not (extra & JUNK & blob_tokens):
        return False
    if not bt <= et:
        return False
    if brand_n in COMMON and extra and not (extra & blob_tokens):
        return False
    return True
