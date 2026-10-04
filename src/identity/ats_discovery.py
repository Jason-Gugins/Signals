"""ATS vendor + board-token discovery. detect_ats and the sitemap parsers are pure.

Careers-page lookup order (see ``AtsDiscovery.discover``): an existing
``careers_url`` -> the site's own sitemap map (``src/identity/sitemap_careers``)
-> homepage link hop -> hardcoded path guesses -> listing-hub hop -> verified
board-candidate ladder (keyless JSON APIs only).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Optional
from urllib.parse import urljoin, urlparse

from src.core.models import Account
from src.identity.sitemap_careers import SitemapCareersFinder, fetcher_for

if TYPE_CHECKING:
    from src.core.http import HttpFetcher
    from src.identity.registry import AccountRegistry


# Per-account request ceiling for AtsDiscovery.discover. The sitemap stage is
# greedy by design (robots.txt -> <=3 root sitemaps -> <=3 child sitemaps ->
# the careers page), so the budget is larger than the old 4: the map stages run
# first, the leftover budget is what the homepage hop and the hardcoded guesses
# get, and the new rungs only spend what the marker rungs left behind — one
# listing-hub fetch plus up to MAX_VERIFY_REQUESTS board-API probes (12 =
# round 1 (t1 x N pinned vendors) + as much of round 2 (t2 x N) as the cap
# allows). The raise costs <=12 extra 404 GETs per UNDETECTED account per
# resolve — misses are unstored 404s, and stamped accounts never probe again —
# with hosts paced by the 1 req/s/host limiter.
MAX_DISCOVERY_REQUESTS = 22

ATS_PATTERNS: dict[str, list[re.Pattern]] = {
    "greenhouse": [
        re.compile(r"boards\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)", re.I),
        re.compile(r"job-boards\.greenhouse\.io/([a-z0-9_-]+)", re.I),
    ],
    "lever": [re.compile(r"jobs\.lever\.co/([a-z0-9_-]+)", re.I)],
    "ashby": [re.compile(r"jobs\.ashbyhq\.com/([a-z0-9_-]+)", re.I)],
    "smartrecruiters": [
        re.compile(r"careers\.smartrecruiters\.com/([A-Za-z0-9_-]+)"),
        re.compile(r"jobs\.smartrecruiters\.com/([A-Za-z0-9_-]+)"),
    ],
    "workable": [
        re.compile(r"([a-z0-9-]+)\.workable\.com", re.I),
        re.compile(r"apply\.workable\.com/([a-z0-9-]+)", re.I),
    ],
    "recruitee": [re.compile(r"([a-z0-9-]+)\.recruitee\.com", re.I)],
    "workday": [
        re.compile(
            r"([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)",
            re.I,
        )
    ],
    "bamboohr": [re.compile(r"([a-z0-9-]+)\.bamboohr\.com/(?:jobs|careers)", re.I)],
    "jazzhr": [re.compile(r"([a-z0-9-]+)\.applytojob\.com", re.I)],
    "personio": [re.compile(r"([a-z0-9-]+)\.jobs\.personio\.(?:de|com)", re.I)],
    "teamtailor": [re.compile(r"([a-z0-9-]+)\.teamtailor\.com", re.I)],
    # Collector-backed vendors (src/sources/registry.py COLLECTED_VENDORS):
    # every one of these must be detectable, else the collector never fires.
    "rippling": [re.compile(r"ats\.rippling\.com/([a-z0-9_-]+)", re.I)],
    "jobvite": [re.compile(r"jobs\.jobvite\.com/([a-z0-9_-]+)", re.I)],
    "breezy": [re.compile(r"([a-z0-9-]+)\.breezy\.hr", re.I)],
}


RESERVED_ATS_TOKENS = frozenset(
    {
        "www",
        "apply",
        "jobs",
        "job",
        "api",
        "careers",
        "career",
        "status",
        "help",
        "support",
        "resources",
        "static",
        "assets",
        "cdn",
        "blog",
    }
)
# Infrastructure labels that are never a board token. Deliberately small: a
# false negative here only leaves ats_vendor/ats_token empty, so the
# ats_careers_page fallback keeps collecting; a false positive silently stops
# hiring collection for that account.


@dataclass
class AtsMatch:
    vendor: str
    token: str
    evidence_url: str
    confidence: float
    extra: dict = field(default_factory=dict)


class _LinkCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.strong: list[str] = []
        self.scripts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        ad = {k: v or "" for k, v in attrs}
        if tag == "iframe" and ad.get("src"):
            self.strong.append(ad["src"])
        elif tag == "a" and ad.get("href"):
            self.strong.append(ad["href"])
        elif tag == "script" and ad.get("src"):
            self.scripts.append(ad["src"])


def _workday_extra(m: re.Match) -> tuple[str, dict]:
    tenant, wd, site = m.group(1), m.group(2), m.group(3)
    return tenant, {"tenant": tenant, "wd": wd, "site": site}


def detect_ats(html: str, base_url: str) -> list[AtsMatch]:
    """PURE. Scans hrefs, iframes, script srcs, and raw text."""
    collector = _LinkCollector()
    try:
        collector.feed(html)
    except Exception:
        pass
    buckets = [
        (collector.strong, 0.95),
        (collector.scripts, 0.85),
        ([html], 0.5),
    ]
    found: dict[tuple[str, str], AtsMatch] = {}
    for urls, conf in buckets:
        for url in urls:
            for vendor, pats in ATS_PATTERNS.items():
                for pat in pats:
                    for m in pat.finditer(url):
                        extra = {}
                        if vendor == "workday" and m.lastindex and m.lastindex >= 3:
                            token, extra = _workday_extra(m)
                        else:
                            token = m.group(1)
                        # Subdomain-style patterns match the HOST label, so a
                        # reserved label yields a garbage token ("apply" from
                        # apply.workable.com) that would be stamped as ats_token
                        # and point the collector at a nonexistent board.
                        if token.casefold() in RESERVED_ATS_TOKENS:
                            continue
                        key = (vendor, token.casefold())
                        evidence = url if url != html else base_url
                        if len(evidence) > 500:
                            evidence = base_url
                        prev = found.get(key)
                        if prev is None or conf > prev.confidence:
                            found[key] = AtsMatch(
                                vendor=vendor,
                                token=token,
                                evidence_url=evidence,
                                confidence=conf,
                                extra=extra,
                            )
    return sorted(found.values(), key=lambda x: (-x.confidence, x.vendor))


def careers_url_candidates(domain: str) -> list[str]:
    """PURE. Last-resort hardcoded guesses — only used after the sitemap stage
    and the homepage hop both failed to produce a careers page."""
    d = domain.casefold().removeprefix("www.")
    return [
        f"https://{d}/careers",
        f"https://{d}/jobs",
        f"https://{d}/company/careers",
        f"https://careers.{d}/",
        f"https://jobs.{d}/",
        f"https://{d}/about/careers",
        f"https://{d}/join-us",
    ]


# Verified-candidate board ladder (Stage 6): cap on distinct tokens x vendors.
MAX_BOARD_CANDIDATES = 6


def _slug_words(text: str) -> list[str]:
    """PURE. Lowercase alphanumeric word pieces of a brand/domain string."""
    return [w for w in re.split(r"[^a-z0-9]+", (text or "").casefold()) if w]


def board_token_candidates(
    name: str | None, domain: str | None, aliases: list[str] | None = None
) -> list[str]:
    """PURE. Bounded board-token candidates, best-first.

    Order: domain prefix, then each alias (joined form first, then its
    individual words), then the name (joined form first, then its words).
    Reserved labels, sub-3-char tokens and
    duplicates dropped; capped at MAX_BOARD_CANDIDATES. Verified acceptance
    happens later — a wrong candidate costs one 404, never a stamp, UNLESS
    another company owns that exact live board token (generic words carry a
    small collision risk; the length floor shrinks it).
    """
    seen: list[str] = []

    def push(word_list: list[str]) -> None:
        joined = "".join(word_list)
        for tok in ([joined] if len(word_list) > 1 else []) + word_list:
            tok = tok.strip("-_")
            # Generic 2-char tokens ("ai") are collision noise: if another
            # company owns that live board token, verification would stamp it.
            if len(tok) < 3:
                continue
            if tok and tok not in seen and tok not in RESERVED_ATS_TOKENS:
                seen.append(tok)

    host = (domain or "").casefold().removeprefix("www.")
    d_words = _slug_words(host.split(".")[0])  # "abnormal.ai" -> ["abnormal"]
    push(d_words)
    # Alias-first: human-curated former-brand tokens outrank current-name
    # variants, so the ladder reaches the rebranded board token early.
    for alias in aliases or []:
        push(_slug_words(alias))
    push(_slug_words(name or ""))
    return seen[:MAX_BOARD_CANDIDATES]


# Verified-candidate ladder. JSON APIs ONLY — verified live 2026-10-03 that
# HTML board URLs are untrustworthy (jobs.ashbyhq.com returns an identical
# 200 SPA shell for real and garbage tokens). Predicates pinned by probe:
#   greenhouse     200 + non-empty "jobs"   (NEG 404 {"status":404,...})
#   lever          200 + JSON array         (NEG 404 {"ok":false,...})
#   ashby          200 + non-empty "jobs"   via posting-api (NEG 404)
#   workable       200 + non-empty "jobs"   (POS doist)
#   smartrecruiters 200 + totalFound > 0    (200/empty exists for wrong tokens!)
#   breezy         200 + JSON array (or {"positions"/"jobs": [...]})
#                  (POS euler, NEG 404 text/html shell, not JSON)
#   recruitee      200 + non-empty "offers" (POS tether, NEG 404 {"error": ...})
#   teamtailor     200 + non-empty "items" (JSON Feed) or "jobs"
#                  (POS recruitgo, NEG 404 empty body)
# Budget 12 = round 1 (t1 x N pinned vendors) + as much of round 2 (t2 x N)
# as the cap allows — with today's 8 pinned vendors round 1 completes and the
# first 4 probes of round 2 fire, so the alias-token showcase
# (greenhouse, abnormalsecurity) lands at probe #9. Cost: <=12 extra 404 GETs
# per UNDETECTED account per resolve; hosts stay paced by the limiter and
# stamped accounts never probe again.
MAX_VERIFY_REQUESTS = 12

_VERIFY_ENDPOINTS: dict[str, str] = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{t}/jobs",
    "lever": "https://api.lever.co/v0/postings/{t}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{t}",
    "workable": "https://apply.workable.com/api/v1/widget/accounts/{t}?details=true",
    "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{t}/postings",
    "breezy": "https://{t}.breezy.hr/json",  # pinned 2026-10-04, POS=euler, NEG=404 text/html shell (not JSON)
    "recruitee": "https://{t}.recruitee.com/api/offers/",  # pinned 2026-10-04, POS=tether, NEG=404 {"error": ...}
    "teamtailor": "https://{t}.teamtailor.com/jobs.json",  # pinned 2026-10-04, POS=recruitgo, NEG=404 empty body
}

# Diagonal rotation (Stage 6): the 5 probe-pinned majors first, in this exact
# order, then the remaining pinned vendors appended after them — appending
# keeps a major from ever being displaced out of round 1. Invariant: every
# entry is a key of _VERIFY_ENDPOINTS AND a member of COLLECTED_VENDORS
# (pinned by test_verify_board_endpoints_module_invariants).
_LADDER_VENDORS: tuple[str, ...] = (
    "greenhouse",
    "lever",
    "ashby",
    "workable",
    "smartrecruiters",
    # breezy/recruitee/teamtailor: probe-pinned 2026-10-04 (Task 1 kept all
    # three) — appended after the majors so they widen coverage without
    # reshuffling it.
    "breezy",
    "recruitee",
    "teamtailor",
)


def _board_payload_ok(vendor: str, body: str) -> bool:
    """PURE. Vendor-specific acceptance on the JSON payload text.

    Live-probed 2026-10-04: breezy boards are a top-level JSON list of
    positions (parse_breezy also reads {"positions"/"jobs": [...]}); a
    wrong token 404s with an HTML shell. recruitee wraps its offers in
    {"offers": [...]} — the only key parse_recruitee reads; a wrong token
    404s with {"error": ...}. teamtailor /jobs.json is a JSON Feed 1.1
    document (jobs under "items"; the embedded board state parse_teamtailor
    reads uses "jobs"); a wrong token 404s with an empty body. Anything
    unparseable or empty is a MISS for every vendor.
    """
    try:
        data = json.loads(body)
    except Exception:
        return False
    if vendor == "smartrecruiters":
        return isinstance(data, dict) and int(data.get("totalFound") or 0) > 0
    if isinstance(data, list):
        return len(data) > 0
    if isinstance(data, dict):
        if vendor == "breezy":
            return bool(data.get("positions") or data.get("jobs"))
        if vendor == "recruitee":
            return bool(data.get("offers"))
        if vendor == "teamtailor":
            # JSON Feed (the live jobs.json shape) or embedded board state.
            return bool(data.get("items") or data.get("jobs"))
        return bool(data.get("jobs"))
    return False


def verify_board_candidates(fetch_text, candidates: list[tuple[str, str]]) -> Optional[AtsMatch]:
    """Probe (vendor, token) pairs in order; return the first verified AtsMatch.

    fetch_text is the SAME budgeted closure discover() uses (fetch_log source
    'ats_discovery'). Bounded by MAX_VERIFY_REQUESTS. Never raises.
    """
    used = 0
    for vendor, token in candidates:
        template = _VERIFY_ENDPOINTS.get(vendor)
        if template is None or used >= MAX_VERIFY_REQUESTS:
            continue
        url = template.format(t=token)
        used += 1
        try:
            body = fetch_text(url)
        except Exception:
            continue
        try:
            # The payload predicate runs OUTSIDE the fetch try, so a hostile
            # JSON body (e.g. totalFound = 1e999 parses to float inf and
            # int() overflows) must be a MISS for the documented
            # never-raises contract to hold.
            if body and _board_payload_ok(vendor, body):
                return AtsMatch(
                    vendor=vendor, token=token, evidence_url=url,
                    confidence=0.75, extra={"source": "candidate_ladder"},
                )
        except (TypeError, ValueError, OverflowError):
            continue
    return None


_CAREER_HREF = re.compile(r"/(careers|jobs|join-us|company/careers)(?:/|$)", re.I)


def _is_site_root(url: Optional[str]) -> bool:
    """PURE. True for a bare origin URL (no path, or just '/')."""
    return urlparse(url or "").path in ("", "/")


def _best_match_page(
    pages: list[tuple[str, str]],
) -> tuple[Optional[AtsMatch], Optional[str]]:
    """PURE. Best (match, page_url) across fetched pages; (None, None) if none."""
    best: Optional[AtsMatch] = None
    best_url: Optional[str] = None
    for url, html in pages:
        matches = detect_ats(html, url)
        if matches and (best is None or matches[0].confidence > best.confidence):
            best = matches[0]
            best_url = url
    return best, best_url


def _has_careers_shape_page(pages: list[tuple[str, str]]) -> bool:
    """PURE. True when a page OTHER than the bare site root matched.

    A match on the root is not a careers page (ATS links usually sit in homepage
    markup), so discovery must keep probing the legacy rungs for a better URL.
    """
    for url, html in pages:
        if detect_ats(html, url) and not _is_site_root(url):
            return True
    return False


def _has_collected_match(pages: list[tuple[str, str]]) -> bool:
    """PURE. True when any scanned page matched a COLLECTED_VENDORS vendor."""
    from src.sources.registry import COLLECTED_VENDORS

    for url, html in pages:
        matches = detect_ats(html, url)
        if any(m.vendor in COLLECTED_VENDORS for m in matches):
            return True
    return False


def find_careers_url(fetch_text, domain: str, *, max_requests: int = MAX_DISCOVERY_REQUESTS):
    """I/O via injected fetch_text. Full careers ladder, no registry writes.

    Rungs, first hit wins: the site's own sitemap map, then the homepage's
    first careers-looking link, then the hardcoded path candidates. The rung
    that hit is reported on CareersLookup.source as one of robots_sitemap,
    root_sitemap, homepage_link, candidate, or none.
    """
    used = 0

    def fetch(url: str) -> Optional[str]:
        nonlocal used
        if used >= max_requests:
            return None
        used += 1
        return fetch_text(url)

    lookup = SitemapCareersFinder(fetch, max_requests=max(0, max_requests - used)).find(domain)
    if lookup.careers_url:
        lookup.requests = used
        return lookup
    home = f"https://{domain}/"
    html = fetch(home)
    if html:
        hop = _first_careers_link(html, home)
        if hop:
            lookup.careers_url = hop
            lookup.source = "homepage_link"
            lookup.requests = used
            return lookup
    for candidate in careers_url_candidates(domain):
        if fetch(candidate):
            lookup.careers_url = candidate
            lookup.source = "candidate"
            lookup.requests = used
            return lookup
    lookup.requests = used
    return lookup


class AtsDiscovery:
    def __init__(self, fetcher: "HttpFetcher", registry: "AccountRegistry"):
        self.fetcher = fetcher
        self.registry = registry

    def discover(
        self, account: Account, alias_names: list[str] | None = None
    ) -> Optional[AtsMatch]:
        used = 0
        max_req = MAX_DISCOVERY_REQUESTS
        raw_get = fetcher_for(self.fetcher, account.domain, source="ats_discovery")

        def fetch(url: str) -> Optional[str]:
            """Budgeted fetch: returns HTML text or None. Never raises."""
            nonlocal used
            if used >= max_req:
                return None
            used += 1
            return raw_get(url)

        pages: list[tuple[str, str]] = []
        sitemap_careers_url: Optional[str] = None
        # Careers-shaped URLs found by the legacy rungs (homepage hop, path
        # candidates), recorded only AFTER a successful fetch so they are safe
        # to persist - unlike a sitemap guess we never contacted.
        ladder_careers_url: Optional[str] = None
        stored_careers_ok = False

        # Stage 1: an already-known careers URL is authoritative — verify only.
        if account.careers_url:
            html = fetch(account.careers_url)
            if html:
                pages.append((account.careers_url, html))
                stored_careers_ok = True

        # Stage 2: the site's own map — robots.txt -> sitemaps -> best careers URL.
        if not pages:
            lookup = SitemapCareersFinder(
                fetch, max_requests=max(0, max_req - used)
            ).find(account.domain)
            if lookup.careers_url:
                html = fetch(lookup.careers_url)
                if html:
                    # Only a URL we actually fetched counts as discovered:
                    # persisting an unverified sitemap guess would replace a
                    # stored careers_url with something that 404s.
                    sitemap_careers_url = lookup.careers_url
                    pages.append((lookup.careers_url, html))

        # Stage 3: homepage + first careers-looking link (legacy path).
        if not any(detect_ats(html, url) for url, html in pages):
            home = f"https://{account.domain}/"
            html = fetch(home)
            if html:
                pages.append((home, html))
                hop = _first_careers_link(html, home)
                if hop:
                    hop_html = fetch(hop)
                    if hop_html:
                        pages.append((hop, hop_html))
                        ladder_careers_url = ladder_careers_url or hop

        # Stage 4: last-resort hardcoded guesses. Also runs when the ONLY match
        # so far is on the bare site root - a root match is not a careers page,
        # so keep probing while the budget allows.
        if not _has_careers_shape_page(pages):
            for cand in careers_url_candidates(account.domain):
                if used >= max_req:
                    break
                html = fetch(cand)
                if not html:
                    continue
                pages.append((cand, html))
                ladder_careers_url = ladder_careers_url or cand
                if detect_ats(html, cand):
                    break

        # Stage 5: listing-hub hop. SPA career sites split the branded careers
        # index (marker-free) from the page that embeds the ATS widget; follow
        # ONE hub link and scan it. Costs 1 request, only when markers failed.
        if not _has_collected_match(pages):
            fetched_urls = {p_url for p_url, _ in pages}
            hub = None
            for url, html in pages:
                candidate = listing_hub_link(html, url)
                # A careers page can link to itself as its own hub — skip any
                # hub URL we already hold and keep scanning the remaining
                # pages; refetching the same HTML spends a request for nothing.
                if candidate and candidate not in fetched_urls:
                    hub = candidate
                    break
            if hub and used < max_req:
                hub_html = fetch(hub)
                if hub_html:
                    pages.append((hub, hub_html))

        # Stage 6: verified-candidate board ladder. JSON APIs only (HTML boards
        # are untrustworthy catch-alls — see _VERIFY_ENDPOINTS note). Tokens from
        # name/domain/entity aliases; acceptance = vendor's canonical API shape.
        if not _has_collected_match(pages):
            tokens = board_token_candidates(
                account.name, account.domain, aliases=alias_names or []
            )
            # Diagonal crossing: token-outer, vendor-inner. Every account
            # probes its domain-prefix token on ALL 8 pinned vendors in round
            # 1 — the old vendor-major order deterministically starved
            # workable/smartrecruiters/ashby (8 probes bought greenhouse x 6 +
            # lever x 2 on the Abnormal input). Round 2 reaches the alias
            # tokens — the rebrand case this ladder exists for. Budget 12
            # covers round 1 complete plus the first 4 probes of round 2; the
            # showcase (greenhouse, abnormalsecurity) lands at probe #9 with
            # 8 vendors.
            candidates = [(v, t) for t in tokens for v in _LADDER_VENDORS]
            match = verify_board_candidates(fetch, candidates)
            if match:
                account.ats_vendor = match.vendor
                account.ats_token = match.token
                if not account.careers_url:
                    # Root guard: a ladder hit verified a board API, not a
                    # careers page, and the last fetched page can be the bare
                    # site root — never stamp that as careers_url. Fill from
                    # the last non-root page fetched, else leave it unset.
                    for url, _html in reversed(pages):
                        if not _is_site_root(url):
                            account.careers_url = url
                            break
                self.registry.upsert(account, source="ats_discovery")
                return match

        best, best_url = _best_match_page(pages)
        # Never record the bare site root when a careers-shaped URL is known: a
        # footer or inline ATS link on the homepage must not discard the careers
        # page this feature exists to find (ats_careers_page scrapes it).
        careers_url = best_url or account.careers_url
        if _is_site_root(best_url):
            careers_url = (
                account.careers_url
                or sitemap_careers_url
                or ladder_careers_url
                or best_url
            )
        if best:
            token = best.token
            if best.vendor == "workday" and best.extra.get("tenant"):
                token = f"{best.extra['tenant']}/{best.extra['wd']}/{best.extra['site']}"
            # Only stamp vendor/token for vendors that actually have a
            # collector. Collector-less detections (bamboohr/jazzhr/personio
            # today) must stay vendorless: setting ats_vendor OR ats_token
            # disables the ats_careers_page fallback (_ats_vendor_ok requires
            # BOTH empty), which would silently stop hiring collection.
            from src.sources.registry import COLLECTED_VENDORS

            if best.vendor in COLLECTED_VENDORS:
                account.ats_vendor = best.vendor
                account.ats_token = token
            account.careers_url = careers_url
            self.registry.upsert(account, source="ats_discovery")
        elif (sitemap_careers_url or ladder_careers_url) and (
            not account.careers_url or not stored_careers_ok
        ):
            # No ATS on the site, but we still learned where the careers index
            # lives - persist it so ats_careers_page stops scraping a guessed
            # /careers. Every URL recorded here was fetched successfully, and a
            # working stored value is never replaced (that would flap between
            # two valid URLs on every run). Vendor/token stay empty on purpose.
            discovered = sitemap_careers_url or ladder_careers_url
            if discovered != account.careers_url:
                account.careers_url = discovered
                self.registry.upsert(account, source="ats_discovery")
        return best


def _first_careers_link(html: str, base: str) -> str | None:
    collector = _LinkCollector()
    try:
        collector.feed(html)
    except Exception:
        return None
    for href in collector.strong:
        if _CAREER_HREF.search(urlparse(href).path or href):
            return urljoin(base, href)
    return None


_HUB_HREF = re.compile(
    r"/(?:open[-_]?(?:roles|positions)|search[-_]jobs|vacancies|jobs?|join[-_]us)(?:[/?#]|$)", re.I
)


def listing_hub_link(html: str, base_url: str) -> Optional[str]:
    """PURE. First internal link shaped like a job-listing hub, query stripped.

    Distinct from _first_careers_link (which finds the careers INDEX from the
    homepage): this runs ON the careers page to find the page that actually
    lists roles — modern SPA career sites split index from listings.
    """
    collector = _LinkCollector()
    try:
        collector.feed(html)
    except Exception:
        return None
    for href in collector.strong:
        if href.startswith(("#", "mailto:", "javascript:")):
            continue
        absolute = urljoin(base_url, href)
        if urlparse(absolute).netloc != urlparse(base_url).netloc:
            continue
        path = urlparse(absolute).path
        if _HUB_HREF.search(path or ""):
            return absolute.split("?")[0].split("#")[0]
    return None
