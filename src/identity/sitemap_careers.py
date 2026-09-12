"""Careers-URL discovery via robots.txt + XML sitemaps.

Instead of guessing `/careers`, `/jobs`, ... we read the site's own published
map: robots.txt `Sitemap:` directives, then the sitemap (or sitemap index) they
point at, then score every URL in those sitemaps for careers-index shape.

Pure helpers (no I/O, no clock): parse_robots_sitemaps, parse_sitemap,
career_path_score, pick_careers_url. All I/O lives in SitemapCareersFinder,
which takes an injected ``fetch_text(url) -> str | None`` callable so tests can
run it against a dict.

Robots policy: this module never bypasses robots — the caller's fetcher already
applies it (config.http.respect_robots + robots_allow overrides). A host that
disallows /robots.txt or /sitemap.xml simply yields no sitemap and the caller
falls back to its legacy heuristics.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import islice as _islice
from typing import Callable, Iterable, Optional
from urllib.parse import urljoin, urlparse

# --- robots.txt ---------------------------------------------------------------

_ROBOTS_SITEMAP = re.compile(r"^[ \t]*sitemap[ \t]*:[ \t]*(\S+)[ \t]*$", re.I | re.M)


def parse_robots_sitemaps(text: str, base_url: str) -> list[str]:
    """PURE. `Sitemap:` directive URLs from robots.txt, absolutized, deduped."""
    if not text:
        return []
    out: list[str] = []
    for match in _ROBOTS_SITEMAP.finditer(text):
        url = urljoin(base_url, match.group(1).strip())
        if url.startswith("http") and url not in out:
            out.append(url)
    return out


# --- sitemap XML --------------------------------------------------------------

_LOC = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.I | re.S)
_ROOT_TAG = re.compile(r"<\s*(sitemapindex|urlset)\b", re.I)
_BARE_URL_LINE = re.compile(r"^\s*https?://\S+\s*$", re.I | re.M)
MAX_LOCS_PER_SITEMAP = 50_000


@dataclass(frozen=True)
class SitemapDoc:
    kind: str  # "urlset" | "sitemapindex" | "text" | "unknown"
    urls: tuple[str, ...] = ()
    sitemaps: tuple[str, ...] = ()


def parse_sitemap(xml_text: str) -> SitemapDoc:
    """PURE. Classify + extract <loc> entries from one sitemap document.

    The root element decides the meaning of <loc>: urlset = page URLs,
    sitemapindex = child sitemap URLs. Nameless plain-text sitemaps (one URL
    per line) are accepted as kind="text". Never raises: malformed or
    gzip-compressed bodies yield kind="unknown" with empty tuples.
    """
    xml_text = xml_text.lstrip("\ufeff")
    if not xml_text:
        return SitemapDoc("unknown")
    head = xml_text[:2000]
    found = _ROOT_TAG.search(head)
    kind = found.group(1).casefold() if found else None
    locs = tuple(
        loc
        for loc in (
            match.group(1).strip()
            for match in _islice(_LOC.finditer(xml_text), MAX_LOCS_PER_SITEMAP)
        )
        if loc
    )
    if kind is None:
        if locs:
            kind = "urlset"
        else:
            lines = tuple(
                line
                for line in (
                    raw.strip()
                    for raw in _islice(xml_text.splitlines(), MAX_LOCS_PER_SITEMAP)
                )
                if line.startswith("http")
            )
            if _BARE_URL_LINE.search(xml_text) and lines:
                return SitemapDoc("text", urls=lines)
            return SitemapDoc("unknown")
    if kind == "sitemapindex":
        return SitemapDoc("sitemapindex", sitemaps=locs)
    return SitemapDoc("urlset", urls=locs)


# --- careers-URL scoring ------------------------------------------------------

_HINTS: tuple[tuple[str, int], ...] = (
    ("careers", 100),
    ("joinus", 100),
    ("jobs", 95),
    ("workwithus", 95),
    ("openroles", 90),
    ("openings", 85),
    ("opportunities", 75),
    ("positions", 65),
    ("vacancies", 65),
    ("join", 60),
    ("hiring", 60),
    ("employment", 50),
)
_MIN_INDEX_SCORE = 60
_REJECT_EXT = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".css", ".js", ".xml", ".gz")


def career_path_score(url: str) -> int:
    """PURE. Score a URL as a careers *index* page. Negative = reject.

    Weight of the best hint per path segment, minus 10 per level of depth
    (deeper paths are rarely the index), minus 15 when any segment carries
    digits (job-12345 detail pages). Paths deeper than 3 segments and
    non-HTML asset extensions are always rejected.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return -1
    path = parsed.path.casefold().rstrip("/")
    if not path or path.endswith(_REJECT_EXT):
        return -1
    segments = [s for s in re.split(r"[/_.\-]+", path) if s]
    if not segments or len(segments) > 3:
        return -1
    score = 0
    for depth, segment in enumerate(segments):
        best = 0
        for hint, weight in _HINTS:
            if segment == hint or (len(hint) >= 5 and segment.startswith(hint)):
                best = max(best, weight)
        if best:
            score += best - 10 * depth
    if score <= 0:
        return -1
    if any(ch.isdigit() for segment in segments for ch in segment):
        score -= 15
    return score if score >= _MIN_INDEX_SCORE else -1


def pick_careers_url(urls: Iterable[str]) -> Optional[str]:
    """PURE. Best careers-index URL from a pool. Deterministic.

    Rank: (score desc, path depth asc, path length asc, url asc), so the
    shallowest highest-scoring index always wins regardless of input order.
    """
    best: Optional[tuple[tuple[int, int, int, str], str]] = None
    for url in dict.fromkeys(urls):
        score = career_path_score(url)
        if score < _MIN_INDEX_SCORE:
            continue
        depth = len([s for s in urlparse(url).path.split("/") if s])
        rank = (-score, depth, len(urlparse(url).path), url)
        if best is None or rank < best[0]:
            best = (rank, url)
    return best[1] if best else None


# --- finder (all I/O injected) ------------------------------------------------

DEFAULT_MAX_SITEMAPS = 3
DEFAULT_MAX_REQUESTS = 6
_CAREERISH_SITEMAP = re.compile(r"(career|job|position|hiring|vacanc)", re.I)
_GENERIC_PAGE_CHILD = re.compile(r"(^|[/_-])(page|pages)[-_]", re.I)
_TAXONOMY_CHILD = re.compile(
    r"^(category|topic|tag|author|glossary|industry|location|resource_type|integration_type|post_tag)[-_]",
    re.I,
)
FetchText = Callable[[str], Optional[str]]


@dataclass
class CareersLookup:
    careers_url: Optional[str] = None
    source: str = "none"  # robots_sitemap | root_sitemap | none
    requests: int = 0
    # ATTEMPTED fetches: a failed or unparsable sitemap still appears here.
    sitemaps_fetched: list[str] = field(default_factory=list)
    pages_seen: int = 0


def fetcher_for(fetcher, domain: str, *, source: str = "careers_discovery") -> FetchText:
    """Adapter: HttpFetcher -> Callable[[url], html | None].

    Returns None for a genuine fetch failure (404, robots-blocked, or a
    transport error, which is logged at warning level). Re-raises ConfigError:
    a misconfigured config fails every fetch identically, and swallowing it
    once made the CLI print a confident "no careers page" for a site we had
    never actually contacted.
    """

    def _get(url: str) -> Optional[str]:
        from loguru import logger

        from src.core.config import ConfigError
        from src.identity.edgar_ids import _Task

        try:
            res = fetcher.get(_Task(source=source, url=url, domain=domain))
        except ConfigError:
            # A configuration error would fail every subsequent fetch
            # identically; surface it instead of reporting "no careers page".
            raise
        except Exception as exc:
            logger.warning("careers fetch failed for {}: {}", url, exc)
            return None
        if res and res.ok and res.doc and res.doc.body:
            return res.doc.body.decode("utf-8", "replace")
        return None

    return _get


class SitemapCareersFinder:
    """robots.txt -> sitemap(s) -> best careers-index URL.

    ``fetch_text`` is injected (see ``fetcher_for``). Request accounting is
    internal and reported on the result so callers sharing one budget can
    decide what to do next; it never exceeds ``max_requests``.
    Child sitemaps are read one level deep and gzipped children are skipped.
    """

    def __init__(
        self,
        fetch_text: FetchText,
        *,
        max_sitemaps: int = DEFAULT_MAX_SITEMAPS,
        max_requests: int = DEFAULT_MAX_REQUESTS,
    ):
        self.fetch_text = fetch_text
        self.max_sitemaps = max_sitemaps
        self.max_requests = max_requests

    def _get(self, url: str, lookup: CareersLookup) -> Optional[str]:
        if lookup.requests >= self.max_requests:
            return None
        lookup.requests += 1
        try:
            return self.fetch_text(url)
        except Exception:
            return None

    def find(self, domain: str) -> CareersLookup:
        lookup = CareersLookup()
        clean = (domain or "").casefold().removeprefix("www.").strip("/")
        if not clean:
            return lookup
        base = f"https://{clean}/"

        robots_url = urljoin(base, "robots.txt")
        roots = parse_robots_sitemaps(self._get(robots_url, lookup) or "", robots_url)
        lookup.source = "robots_sitemap" if roots else "root_sitemap"
        fallback = urljoin(base, "sitemap.xml")
        if fallback not in roots:
            roots.append(fallback)

        pool: list[str] = []
        pending_children: list[str] = []
        for root in roots[: self.max_sitemaps]:
            if lookup.requests >= self.max_requests:
                break
            doc = parse_sitemap(self._get(root, lookup) or "")
            lookup.sitemaps_fetched.append(root)
            if doc.kind in {"urlset", "text"}:
                pool.extend(doc.urls)
            elif doc.kind == "sitemapindex":
                pending_children.extend(doc.sitemaps)

        def _child_rank(u: str) -> int:
            if _CAREERISH_SITEMAP.search(u):
                return 0
            if _GENERIC_PAGE_CHILD.search(u):
                return 1
            return 3 if _TAXONOMY_CHILD.search(u) else 2

        children = sorted(
            (
                child
                for child in dict.fromkeys(pending_children)
                if not urlparse(child).path.casefold().endswith(".gz")
            ),
            key=lambda u: (_child_rank(u), u),
        )
        for child in children[: self.max_sitemaps]:
            if lookup.requests >= self.max_requests:
                break
            doc = parse_sitemap(self._get(child, lookup) or "")
            lookup.sitemaps_fetched.append(child)
            pool.extend(doc.urls)

        lookup.pages_seen = len(pool)
        picked = pick_careers_url(pool)
        if picked:
            lookup.careers_url = picked
        else:
            lookup.source = "none"
        return lookup
