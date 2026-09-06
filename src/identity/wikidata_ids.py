"""Discover company domains from Wikidata P856 (official website) claims.

Waterfall stage 1 of the Clay "find the company" clone (plan T3a):
wbsearchentities -> wbgetclaims P856. Intended dispatch: wired into the
resolve waterfall by the parent as a keyless name->domain resolver (this
module does not self-dispatch). A full discovery pass is 1 search GET plus
one claims GET per returned entity (capped at 5) — the P856-exists arm of
the business filter needs claims for every candidate, not just the pick.

The pick is description-keyed (T1 probe: 4/5 "Stripe" search hits are
non-business — a color band, a Gremlins character, a progamer, a beetle
family). Ranking is display-only and never auto-selects among multiple
domain-bearing survivors: exactly one resolves, everything else comes back
as ranked candidates for the review queue (never-guess contract).

Parsers are pure; the resolver is network-backed via the injected fetcher.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urlencode

from loguru import logger

from src.identity.domains import root_domain
from src.identity.names import name_matches_domain, name_tokens

API_URL = "https://www.wikidata.org/w/api.php"
CLAIM_PROPERTY = "P856"

# Casefold-substring terms that mark a label/description as business-ish.
# Short legal forms ("inc", "sa", "ag") are noisy as substrings, but the
# decision rule bounds the damage: a false survivor without a domain cannot
# resolve, and one with a domain only pushes the call to "ambiguous"
# (review queue) instead of guessing.
BUSINESS_TERMS: tuple[str, ...] = (
    "company",
    "inc",
    "corporation",
    "ltd",
    "limited",
    "gmbh",
    "bv",
    "sa",
    "ag",
    "plc",
    "llc",
    "technolog",
    "software",
    "platform",
    "payments",
    "solutions",
    "group",
)


# House style for fetch tasks outside src/sources/: local frozen-ish dataclass
# mirroring appstore_ids._Task.
@dataclass
class _Task:
    source: str
    url: str
    domain: str | None = None
    method: str = "GET"
    headers: dict | None = None
    json_body: dict | None = None

    def __post_init__(self) -> None:
        if self.headers is None:
            self.headers = {}


def build_search_url(name: str, limit: int = 5) -> str:
    return f"{API_URL}?{urlencode({'action': 'wbsearchentities', 'search': name, 'language': 'en', 'type': 'item', 'format': 'json', 'limit': limit})}"


def build_claims_url(qid: str) -> str:
    return f"{API_URL}?{urlencode({'action': 'wbgetclaims', 'entity': qid, 'property': CLAIM_PROPERTY, 'format': 'json'})}"


def parse_wbsearch(payload: bytes) -> list[dict]:
    """Entities from a wbsearchentities payload (pure)."""
    raw = json.loads(payload)
    results = raw.get("search") or []
    return [r for r in results if isinstance(r, dict) and r.get("id")]


def parse_claims(payload: bytes) -> list[str]:
    """P856 official-website URL strings from a wbgetclaims payload (pure).

    The T1 probe artifact pins the real shape as datavalue.value being the
    bare URL string for datatype=url; the nested datavalue.value["value"]
    shape is unwrapped too when it appears.
    """
    raw = json.loads(payload)
    claims = raw.get("claims") or {}
    urls: list[str] = []
    for claim in claims.get(CLAIM_PROPERTY) or []:
        if not isinstance(claim, dict):
            continue
        datavalue = (claim.get("mainsnak") or {}).get("datavalue") or {}
        value = datavalue.get("value")
        if isinstance(value, dict):
            value = value.get("value")
        if isinstance(value, str) and value.strip():
            urls.append(value.strip())
    return urls


def _no_match() -> dict:
    return {"status": "no_match", "domain": None, "candidates": []}


def _is_business(entity: dict) -> bool:
    text = " ".join(
        str(entity.get(key) or "") for key in ("label", "description")
    ).casefold()
    return any(term in text for term in BUSINESS_TERMS)


def _score_entity(
    entity: dict, name: str, p856_url: str | None, p856_domain: str | None
) -> int:
    label = str(entity.get("label") or "")
    score = 0
    if p856_url:
        score += 2
    name_toks = set(name_tokens(name))
    label_toks = set(name_tokens(label))
    two_way = bool(name_toks) and bool(label_toks) and bool(name_toks & label_toks)
    domain_hit = bool(
        p856_domain
        and (
            name_matches_domain(label, p856_domain)
            or name_matches_domain(name, p856_domain)
        )
    )
    if two_way and domain_hit:
        score += 2
    if label.strip().casefold() == name.strip().casefold():
        score += 1
    return score


def pick_wikidata_candidate(entities: list[dict], name: str) -> dict:
    """Filter, score and decide from enriched entities (pure).

    `entities` items carry {"qid", "label", "description", "p856_url"}.
    Filter keeps business-ish entities (label/description term) OR any entity
    with a P856. Decision over survivors that actually yielded a domain:
    zero -> no_match, exactly one -> resolved, more than one -> ambiguous
    with ALL candidates ranked by score (never auto-picks the top one).
    """
    survivors: list[dict] = []
    for entity in entities:
        p856_url = entity.get("p856_url") or None
        p856_domain = root_domain(p856_url)
        if not _is_business(entity) and not p856_url:
            continue
        survivors.append(
            {
                "qid": entity.get("qid"),
                "label": entity.get("label"),
                "description": entity.get("description"),
                "domain": p856_domain,
                "p856_url": p856_url,
                "score": _score_entity(entity, name, p856_url, p856_domain),
            }
        )
    if not survivors:
        return _no_match()
    survivors.sort(key=lambda c: c["score"], reverse=True)
    with_domain = [c for c in survivors if c["domain"]]
    if not with_domain:
        return {"status": "no_match", "domain": None, "candidates": survivors}
    if len(with_domain) == 1:
        return {
            "status": "resolved",
            "domain": with_domain[0]["domain"],
            "candidates": survivors,
        }
    return {"status": "ambiguous", "domain": None, "candidates": survivors}


class WikidataDomainResolver:
    """Name -> company domain from the keyless Wikidata API (HttpFetcher tier).

    `registry` is accepted for house-style parity with the other identity
    resolvers; this module never writes to it — the waterfall wiring layer
    owns persistence and the 2-source agreement rule (never-guess contract).
    """

    def __init__(self, fetcher, registry):
        self.fetcher = fetcher
        self.registry = registry

    def discover(self, name: str) -> dict:
        """Name -> {"status", "domain", "candidates"}. Never raises."""
        try:
            return self._discover(name)
        except Exception as exc:  # network/parse failures never raise
            logger.warning("wikidata_ids: discovery failed for {!r}: {}", name, exc)
            return _no_match()

    def resolve_all(self, names: list[str]) -> dict[str, dict]:
        """Batch entry with per-item try/except isolation (resolve_all style)."""
        out: dict[str, dict] = {}
        for name in names:
            try:
                out[name] = self.discover(name)
            except Exception as exc:  # one failing name never kills the pass
                logger.warning("wikidata_ids: resolve failed for {!r}: {}", name, exc)
                out[name] = _no_match()
        return out

    def _discover(self, name: str) -> dict:
        term = (name or "").strip()
        if not term:
            return _no_match()
        result = self.fetcher.get(_Task(source="wikidata", url=build_search_url(term)))
        if not result.ok or not result.doc or not result.doc.body:
            return _no_match()
        hits = parse_wbsearch(result.doc.body)[:5]
        enriched: list[dict] = []
        for hit in hits:
            qid = str(hit.get("id"))
            p856_url = None
            try:
                claims_result = self.fetcher.get(
                    _Task(source="wikidata", url=build_claims_url(qid))
                )
                if claims_result.ok and claims_result.doc and claims_result.doc.body:
                    urls = parse_claims(claims_result.doc.body)
                    p856_url = urls[0] if urls else None
            except Exception as exc:  # one bad entity never kills the pass
                logger.warning("wikidata_ids: claims fetch failed for {}: {}", qid, exc)
            enriched.append(
                {
                    "qid": qid,
                    "label": str(hit.get("label") or ""),
                    "description": str(hit.get("description") or ""),
                    "p856_url": p856_url,
                }
            )
        return pick_wikidata_candidate(enriched, term)
