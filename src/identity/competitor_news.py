"""Keyless competitor-candidate mining from comparison headlines (plan T7).

Wave 2 of the Clay "find-similar" clone; carries T6's candidate-store
mechanism per the T5 probe re-scope (data/probe/G2_COMPETITORS_2026_09.md):
the G2 /competitors/alternatives pass is deferred (DataDome NO-GO), so ranked
kind=competitor candidates come from keyless news co-mentions instead. The
G2 page pass is NOT built here; only Bing News RSS + HN Algolia.

Three keyless GETs per name, no auth anywhere (hard budget — one query per
source, never more):
1. Bing News RSS  '"X" vs'            (www.bing.com/news/search, format=RSS)
2. Bing News RSS  '"X" alternative to'
3. HN Algolia     '"X" vs OR "alternative to" X'  (tags=story, title text only)

ROBOTS: HttpFetcher checks robots.txt per host before any GET. The T1 probe
confirmed www.bing.com robots.txt ships NO /news rules, so /news/search is
fetchable by the generic UA — no scoped robots_allow override is needed (do
not add one). hn.algolia.com is a public keyless JSON API. news.google.com
RSS is already robots-allowed in config but is deliberately NOT used here.

HN query decision (documented per plan): ONE boolean-OR query
('"X" vs OR "alternative to" X') instead of two simpler queries — HN
Algolia's advanced query syntax supports boolean OR (verified in the review
round), and two HN queries would break the 3-GET budget. If the OR ever
degrades it widens the match — keyless and harmless.

Extraction is co-mention ONLY: a competitor name is never fabricated — it is
a split side of a headline that contained a comparison pattern and does not
normalize to the self name. Every candidate scores 1: this is the
lowest-confidence tier by design and the human gate (identity_candidates
review, then promote into config/lists/competitors.txt or
config/fingerprints.yaml) is MANDATORY. Nothing here self-dispatches:
Orchestrator.discover_competitors owns the fetcher and the store write.

Parsers and the extractor are pure module functions in house resolver style
(per-item try/except, never raises); the pass mirrors
wikidata_ids.WikidataDomainResolver (local _Task dataclass, injected
fetcher, per-SOURCE error isolation).
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import urlencode

from loguru import logger

from src.identity.resolve import normalize_entity

BING_NEWS_URL = "https://www.bing.com/news/search"
HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search"

# Per-source result caps (verified feed shape: ~4 items per Bing query; HN
# page size pinned by hitsPerPage in the URL).
RSS_ITEM_CAP = 10
HN_HIT_CAP = 20

# Hard per-name budget: bing vs + bing alt + HN = 3 GETs, never more.
MAX_FETCHES_PER_NAME = 3


# House style for fetch tasks outside src/sources/: local frozen-ish dataclass
# mirroring wikidata_ids._Task / edgar_ids._Task.
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


# -- query builders (pure) -----------------------------------------------------


def build_bing_vs_url(name: str) -> str:
    """Bing News RSS URL for '"<name>" vs' (pure).

    The exact shape is the review-round-verified one — quotes stay literal in
    the query and are percent-encoded by urlencode: q=%22Stripe%22+vs.
    """
    return f"{BING_NEWS_URL}?{urlencode({'q': f'\"{name}\" vs', 'format': 'RSS'})}"


def build_bing_alt_url(name: str) -> str:
    """Bing News RSS URL for '"<name>" alternative to' (pure).

    Positive syntax kept alongside the vs query: '"X" alternative to'
    produced results in the review round.
    """
    return f"{BING_NEWS_URL}?{urlencode({'q': f'\"{name}\" alternative to', 'format': 'RSS'})}"


def build_hn_url(name: str) -> str:
    """HN Algolia search URL, story tag, title-text query (pure).

    One boolean-OR query ('"X" vs OR "alternative to" X') — see module
    docstring for why this is not two separate queries.
    """
    query = f'\"{name}\" vs OR \"alternative to\" {name}'
    return f"{HN_SEARCH_URL}?{urlencode({'query': query, 'tags': 'story', 'hitsPerPage': HN_HIT_CAP})}"


# -- parsers (pure) ------------------------------------------------------------


def _localname(tag: object) -> str:
    """Namespace-stripped element tag (pure): '{ns}item' -> 'item'."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def parse_rss_titles(xml_bytes: bytes) -> list[dict]:
    """RSS 2.0 channel items as {"title", "url"} (pure), capped at 10.

    Tolerates encoding declarations (ElementTree parses bytes and honors the
    declared encoding) and CDATA sections (handled transparently). Items with
    an empty title are skipped; a missing <link> yields url "". Malformed XML
    -> [] — never raises.
    """
    try:
        root = ET.fromstring(xml_bytes)
    except Exception:  # ParseError and exotic encoding decls -> no results
        return []
    out: list[dict] = []
    for elem in root.iter():
        if _localname(elem.tag) != "item":
            continue
        title = url = ""
        for child in elem:
            name = _localname(child.tag)
            if name == "title" and not title:
                title = (child.text or "").strip()
            elif name == "link" and not url:
                url = (child.text or "").strip()
        if title:
            out.append({"title": title, "url": url})
        if len(out) >= RSS_ITEM_CAP:
            break
    return out


def parse_hn_hits(json_bytes: bytes) -> list[dict]:
    """HN Algolia search hits as {"title", "url"} (pure), capped at 20.

    Title text only (the plan's contract). A hit with url=None keeps its
    title with url ""; hits without a usable title are skipped. Malformed
    JSON -> [] — never raises.
    """
    try:
        raw = json.loads(json_bytes)
    except Exception:  # JSONDecodeError and non-UTF payloads -> no results
        return []
    hits = raw.get("hits") if isinstance(raw, dict) else None
    out: list[dict] = []
    for hit in hits or []:
        if not isinstance(hit, dict):
            continue
        title = str(hit.get("title") or "").strip()
        if not title:
            continue
        out.append({"title": title, "url": str(hit.get("url") or "")})
        if len(out) >= HN_HIT_CAP:
            break
    return out


# -- co-mention extraction (pure) -----------------------------------------------

# Headline comparison patterns, casefold-matched. " vs" / " versus" are
# matched WITHOUT a trailing space (a small documented widening of the spec's
# " vs " / " versus "): end-of-string headlines like "Acme vs" then split
# into an empty other side (dropped) instead of matching nothing, and the
# spec's " vs." is kept for its dot. Sides are whitespace/punct-tokenized, so
# the with-space variants are subsumed.
SPLIT_PATTERNS: tuple[str, ...] = (
    "competing with",
    "alternatives to",
    "alternative to",
    "compared to",
    "compares",
    " versus",
    " vs.",
    " vs",
)

# Tokens that are never part of a candidate company name. A headline side
# contributes only its leading run of non-noise tokens (after stripping
# leading noise), so "stripe for startups" -> "stripe" and "the best" -> "".
# Known cost: multi-word names containing noise tokens truncate ("Bank of
# America" -> "bank") — accepted for the lowest-confidence tier, where the
# mandatory human gate owns precision.
NOISE_TOKENS: frozenset[str] = frozenset(
    {
        "a", "an", "the", "and", "or", "but", "for", "to", "of", "with",
        "in", "on", "at", "by", "from", "that", "this", "these", "those",
        "is", "are", "was", "were", "be", "it", "its", "which", "who",
        "what", "why", "how", "vs", "versus", "best", "top", "new", "better",
        "more", "most", "other", "than", "compared", "competing",
        "alternative", "alternatives",
    }
)

# Headline separators that end a candidate run before tokenizing: punctuation
# and dash separators ("paypal: payments showdown" -> "paypal"). A bare
# hyphen INSIDE a word is kept (ProPublica-style names), only " - " cuts.
_SIDE_CUT = re.compile(r"[,.;:!?]|—|–|\s-\s")

# Same token rule family as identity/resolve.py: split on non-word chars.
_TOKEN_SPLIT = re.compile(r"[^\w]+", re.UNICODE)


def _split_on_patterns(cf_title: str) -> list[str]:
    """Split a casefolded title at every comparison-pattern occurrence (pure).

    Longest patterns are marked first into a covered mask so " vs." wins over
    " vs" where both apply; the segments between covered runs are the sides.
    """
    covered = bytearray(len(cf_title))
    for pattern in sorted(SPLIT_PATTERNS, key=len, reverse=True):
        start = 0
        while True:
            idx = cf_title.find(pattern, start)
            if idx < 0:
                break
            covered[idx : idx + len(pattern)] = b"\x01" * len(pattern)
            start = idx + len(pattern)
    if not any(covered):
        return []  # no comparison pattern -> no co-mention candidates at all
    sides: list[str] = []
    run: list[str] = []
    for i, ch in enumerate(cf_title):
        if covered[i]:
            if run:
                sides.append("".join(run))
                run = []
        else:
            run.append(ch)
    if run:
        sides.append("".join(run))
    return sides


def _side_name(side: str) -> str:
    """Candidate name carried by one headline side (pure).

    Cut at headline punctuation, tokenize, strip leading noise tokens, then
    keep only the run of non-noise tokens. Empty when the side carries no
    name-like run ("the best" -> "", "which" -> "").
    """
    cut = _SIDE_CUT.search(side)
    if cut:
        side = side[: cut.start()]
    tokens = [t for t in _TOKEN_SPLIT.split(side) if t]
    i = 0
    while i < len(tokens) and tokens[i].casefold() in NOISE_TOKENS:
        i += 1
    run: list[str] = []
    for tok in tokens[i:]:
        if tok.casefold() in NOISE_TOKENS:
            break
        run.append(tok)
    return " ".join(run)


def extract_competitor_names(titles: list[dict], self_name: str) -> list[dict]:
    """Co-mention competitor candidates from mined headlines (pure).

    `titles` items carry {"title", "url", "source"} (source is
    "bing_news"|"hn_algolia", tagged by the pass). Every comparison-pattern
    side that does not normalize to the self name contributes its leading
    non-noise token run as a candidate:

        {"name": <normalized>, "title", "url", "source", "score": 1}

    Deduped by (source, name) keeping the first title. Empty/short (<3
    normalized chars) and all-noise sides are not names. The self name is
    NEVER returned and no name absent from a title is ever fabricated.
    Score is always 1 — the lowest-confidence tier by design; the human gate
    is mandatory.
    """
    self_norm = normalize_entity(self_name)
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in titles or []:
        title = str((item or {}).get("title") or "").strip()
        if not title:
            continue
        source = str((item or {}).get("source") or "")
        url = str((item or {}).get("url") or "")
        for side in _split_on_patterns(title.casefold()):
            norm = normalize_entity(_side_name(side))
            if not norm or len(norm) < 3:
                continue
            if norm == self_norm:
                continue  # NEVER include the self name
            key = (source, norm)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {"name": norm, "title": title, "url": url, "source": source, "score": 1}
            )
    return out


# -- the pass (network-backed, never raises) ------------------------------------


def _no_match(name: str, errors: dict[str, str] | None = None) -> dict:
    return {
        "status": "no_match",
        "name": name,
        "competitors": [],
        "errors": errors or {},
    }


class CompetitorNewsPass:
    """Co-mention competitor mining across 3 keyless news sources.

    `fetcher` is an HttpFetcher-like object (``.get(task) -> result with
    .ok/.doc/.body``). `registry` is accepted for house-style parity with the
    other identity passes; this module never writes to it —
    Orchestrator.discover_competitors owns the identity_candidates write
    (kind=competitor, source=competitor_news).

    Budget: exactly 3 GETs per name (bing vs, bing alt, HN). Per-SOURCE error
    isolation: one failing source is recorded under "errors" and the others
    still run. discover() never raises.
    """

    def __init__(self, fetcher, registry=None):
        self.fetcher = fetcher
        self.registry = registry

    def discover(self, name: str) -> dict:
        """Name -> {"status", "name", "competitors", "errors"}. Never raises.

        status is "resolved_candidates" when any competitor candidate was
        mined, else "no_match"; "errors" maps source key -> failure detail
        (empty when every source fetched cleanly).
        """
        try:
            return self._discover(name)
        except Exception as exc:  # network/parse failures never raise
            logger.warning("competitor_news: mining failed for {!r}: {}", name, exc)
            return _no_match(name, {"pass": str(exc)})

    def _discover(self, name: str) -> dict:
        term = (name or "").strip()
        if not term:
            return _no_match(name)
        errors: dict[str, str] = {}
        titles: list[dict] = []
        for key, url, parser, source in (
            ("bing_vs", build_bing_vs_url(term), parse_rss_titles, "bing_news"),
            ("bing_alt", build_bing_alt_url(term), parse_rss_titles, "bing_news"),
            ("hn", build_hn_url(term), parse_hn_hits, "hn_algolia"),
        ):
            try:
                items = self._fetch_items(url, parser)
            except Exception as exc:  # one failing source never kills the pass
                logger.warning("competitor_news: {} fetch failed for {!r}: {}", key, name, exc)
                errors[key] = str(exc)
                continue
            for item in items:
                titles.append(
                    {
                        "title": str(item.get("title") or ""),
                        "url": str(item.get("url") or ""),
                        "source": source,
                    }
                )
        competitors = extract_competitor_names(titles, term)
        return {
            "status": "resolved_candidates" if competitors else "no_match",
            "name": name,
            "competitors": competitors,
            "errors": errors,
        }

    def _fetch_items(self, url: str, parser) -> list[dict]:
        """One GET -> parsed {"title", "url"} items; raises on fetch failure
        so the per-SOURCE handler records it (parsers themselves never raise)."""
        result = self.fetcher.get(_Task(source="competitor_news", url=url))
        if not result.ok or not result.doc or not result.doc.body:
            status = getattr(result, "status", "?")
            raise RuntimeError(f"fetch failed (status {status})")
        return parser(result.doc.body)
