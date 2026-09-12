"""ATS vendor + board-token discovery. detect_ats and the sitemap parsers are pure.

Careers-page lookup order (see ``AtsDiscovery.discover``): an existing
``careers_url`` -> the site's own sitemap map (``src/identity/sitemap_careers``)
-> homepage link hop -> hardcoded path guesses.
"""

from __future__ import annotations

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
# first and the leftover budget is what the homepage hop and the hardcoded
# guesses get. Rate limiting (1 req/s/host) still bounds wall-clock cost.
MAX_DISCOVERY_REQUESTS = 10

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


_CAREER_HREF = re.compile(r"/(careers|jobs|join-us|company/careers)(?:/|$)", re.I)


def _is_site_root(url: Optional[str]) -> bool:
    """PURE. True for a bare origin URL (no path, or just '/')."""
    return urlparse(url or "").path in ("", "/")


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

    def discover(self, account: Account) -> Optional[AtsMatch]:
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

        # Stage 1: an already-known careers URL is authoritative — verify only.
        if account.careers_url:
            html = fetch(account.careers_url)
            if html:
                pages.append((account.careers_url, html))

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

        # Stage 4: last-resort hardcoded guesses.
        if not any(detect_ats(html, url) for url, html in pages):
            for cand in careers_url_candidates(account.domain):
                if used >= max_req:
                    break
                html = fetch(cand)
                if not html:
                    continue
                pages.append((cand, html))
                if detect_ats(html, cand):
                    break

        best: Optional[AtsMatch] = None
        best_url: Optional[str] = None
        for url, html in pages:
            matches = detect_ats(html, url)
            if matches and (best is None or matches[0].confidence > best.confidence):
                best = matches[0]
                best_url = url
        # Persist a real careers index in preference to the bare site root: a
        # footer or inline ATS link on the homepage must not discard the careers
        # page this feature exists to find (ats_careers_page scrapes it).
        careers_url = best_url or account.careers_url
        if _is_site_root(best_url):
            careers_url = account.careers_url or sitemap_careers_url or best_url
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
        elif sitemap_careers_url and sitemap_careers_url != account.careers_url:
            # No ATS on the site, but we still learned where the careers index
            # actually lives — persist it so ats_careers_page stops scraping a
            # guessed /careers. Vendor/token stay empty on purpose (see above).
            account.careers_url = sitemap_careers_url
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
