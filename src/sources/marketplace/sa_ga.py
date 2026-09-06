"""Software Advice + GetApp (Gartner Digital Markets) review adapters. PURE.

Plan Task 16 (Batch 3): the Gartner network's two remaining marketplace
properties, fixture-built against the P3 source spike
(``data/probe/P3_SOURCE_SPIKE_2026_09.md``). Spike verdicts: both hosts are
CF-challenge-blocked on plain httpx but serve SERVER-RENDERED product pages
through the repo's standard curl_cffi fetcher, so both adapters run the
``http`` tier. No live probing here — every test parses the local fixtures
in ``tests/fixtures/marketplace/``.

Both sites embed their review data in ``<script type="application/ld+json">``
blocks, so the parse surface is ``extract_jsonld`` + structural matching —
no bs4, no DOM selectors, nothing build-specific:

- Software Advice product pages
  (``https://www.softwareadvice.com/<category>/<slug>-profile/``): a
  ``@graph`` node typed ``["SoftwareApplication", "Product"]`` carrying
  ``aggregateRating`` (the page rollup — NOT a review) plus a ``review``
  list of per-review ``Review`` nodes: ``author.name`` (+ ``worksFor`` org
  line and ``numberOfEmployees.value`` size band), ``datePublished`` in two
  shapes — month-precision ("July 2026") and full ("January 15, 2026") —
  and a 0-5 ``reviewRating.ratingValue``.
- GetApp product pages (``https://www.getapp.com/<category>/a/<slug>/``):
  a ``SoftwareApplication`` node with ``aggregateRating`` (ratingCount in
  the tens of thousands — NOT a review) plus ``positiveNotes`` /
  ``negativeNotes`` ItemLists whose items carry note text (``name``) and
  sometimes an ``author``. Per-note ratings/dates are ABSENT from the live
  JSON-LD (spike: ``reviewBody`` count 0 in the product head) — parsed as
  ``None``, never invented.

Ratings are already 0-5 on both sites (spike: SA ``bestRating "5"``) — they
pass through untouched, UNLIKE TrustRadius' 0-10 halving. A missing rating
stays ``None``: ``0.0`` would fabricate a terrible review out of nothing.

Persistence contract (derived from code, not re-invented): the runner's
``harvest_reviews`` dispatch ends in ``else: upsert_g2_reviews(...)`` for
any ``marketplace_*`` key that is not g2/capterra/trustradius/appstore, so
``GartnerReview`` mirrors the ``CapterraReview`` attribute set exactly —
that IS the attribute set ``upsert_g2_reviews`` reads bare (pinned by
``test_harvest_reviews_objects_match_upsert_contract``). Site provenance
lands in the ``review_source`` column ("softwareadvice" / "getapp"); the
runner's empty-since bookkeeping and review-velocity trend paths key
``marketplace_*`` adapters generically, so no runner changes are needed.

Both adapters ship ``enabled: false`` (config/sources.yaml): the corpus
overlaps Capterra, which is already collected — the marginal value is
incremental review volume/coverage. An account opts in by seeding
``extra_data['sa_url']`` / ``extra_data['getapp_url']`` with the FULL
product profile URL (the slug embeds the site-internal product id and is
not derivable from domain+name — BBB ``bbb_url`` precedent), so
``requires`` stays empty and ``plan()`` gates on the seeded URL.
``follow_tasks`` is intentionally the base-class no-op: the profile page IS
the review document and the JSON-LD carries no pagination.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional
from urllib.parse import urlparse

from src.core.models import Account, Document
from src.core.textutil import to_iso_date
from src.sources.base import FetchTask, SignalCandidate, SourceAdapter
from src.sources.registry import register

__all__ = [
    "GartnerReview",
    "MarketplaceGetAppSource",
    "MarketplaceSoftwareAdviceSource",
    "extract_jsonld",
    "parse_ga_product",
    "parse_sa_product",
    "product_slug_from_url",
]


# ---------------------------------------------------------------------------
# JSON-LD extraction (fail-open)
# ---------------------------------------------------------------------------

_LDJSON_SCRIPT_RE = re.compile(
    r"<script[^>]*type\s*=\s*[\"']?application/ld\+json[\"']?[^>]*>(.*?)</script>",
    re.I | re.S,
)


def _to_text(html) -> str:
    """bytes/str → str (UTF-8, errors replaced). Never raises."""
    if isinstance(html, (bytes, bytearray)):
        return bytes(html).decode("utf-8", "replace")
    return str(html or "")


def extract_jsonld(html: bytes) -> list[dict]:
    """Parse every well-formed ``application/ld+json`` block. PURE, fail-open.

    Malformed blocks (truncated payloads, template junk) are skipped, never
    raised on — one broken block must not cost the page's remaining
    structured data. Non-dict payloads (bare lists/strings) are dropped:
    the SA/GA surfaces matched below are object-shaped.
    """
    out: list[dict] = []
    for m in _LDJSON_SCRIPT_RE.finditer(_to_text(html)):
        try:
            data = json.loads(m.group(1).strip())
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def _graph_nodes(block: dict):
    """Yield the JSON-LD objects to match inside one ld+json block.

    Software Advice wraps its product node in ``@graph``; GetApp puts the
    node at the top level. Both spellings handled: ``@graph`` list items
    (dicts only) when present, else the block itself.
    """
    graph = block.get("@graph")
    if isinstance(graph, list):
        for node in graph:
            if isinstance(node, dict):
                yield node
        return
    yield block


def product_slug_from_url(url) -> str:
    """Product slug segment from an SA/GA product URL. PURE.

    Software Advice: ``/<category>/<slug>-profile/`` → ``<slug>-profile``
    (the ``-profile`` suffix is part of the site's slug and stays). GetApp:
    ``/<category>/a/<slug>/`` → the segment after the ``a`` collection
    marker. Homepages / empty paths → ``""`` (never guess a slug).
    """
    if not url:
        return ""
    path = urlparse(str(url)).path or ""
    segments = [s for s in path.split("/") if s]
    if not segments:
        return ""
    if "a" in segments:
        idx = segments.index("a")
        return segments[idx + 1] if idx + 1 < len(segments) else ""
    return segments[-1]


# ---------------------------------------------------------------------------
# GartnerReview — the attribute contract upsert_g2_reviews reads
# ---------------------------------------------------------------------------


@dataclass
class GartnerReview:
    """One marketplace review row, Software Advice / GetApp normalized.

    Field-for-field identical to ``CapterraReview`` / ``TrustRadiusReview``:
    the runner's ``else: upsert_g2_reviews(...)`` branch reads these
    attributes bare, so the exact set is pinned by
    ``test_harvest_reviews_objects_match_upsert_contract``.
    """

    review_id: str
    product_slug: str
    reviewer_name: Optional[str] = None
    reviewer_title: Optional[str] = None
    reviewer_company_size: Optional[str] = None
    rating: Optional[float] = None
    review_title: Optional[str] = None
    review_body: Optional[str] = None
    pros: list = field(default_factory=list)
    cons: list = field(default_factory=list)
    posted_at: Optional[str] = None
    review_url: Optional[str] = None
    verified_reviewer: bool = False
    review_source: Optional[str] = None
    # Present in the dataclass contract, but the SA/GA JSON-LD carries no
    # NPS-score or helpful-vote markup (P3 spike), so extraction yields
    # None until real markup maps.
    nps_score: Optional[int] = None
    helpful_votes: Optional[int] = None


_ID_BODY_CHARS = 200  # matches the [:200] candidate-summary window


def _review_id(product_slug: str, reviewer_name, posted_at, body) -> str:
    """Capterra-style stable hash, body-extended. PURE.

    sha256 of (slug, reviewer, date, first 200 body chars), hex[:16].
    SA reviewer names are first names and dates can be month-precision, so
    (reviewer, date) alone would collide — the truncated body
    disambiguates and makes the id move when a review's text is edited.
    """
    return hashlib.sha256(
        f"{product_slug}|{reviewer_name}|{posted_at or ''}|{(body or '')[:_ID_BODY_CHARS]}".encode()
    ).hexdigest()[:16]


def _rating_or_none(value) -> Optional[float]:
    """0-5 float passthrough; absent/unparseable → None (never 0.0)."""
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

_MONTH_YEAR_RE = re.compile(
    r"^(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+(\d{4})$",
    re.I,
)


def _sa_date(text) -> Optional[str]:
    """SA ``datePublished`` → ISO, honest precision. PURE.

    "January 15, 2026" → "2026-01-15" (full day, via ``to_iso_date``);
    "July 2026" → "2026-07" (month precision — no day is invented);
    anything else → None.
    """
    if not text:
        return None
    raw = str(text).strip()
    full = to_iso_date(raw)
    if full:
        return full
    m = _MONTH_YEAR_RE.match(raw)
    if m:
        month = _MONTHS.get(m.group(1).lower())
        if month:
            return f"{int(m.group(2)):04d}-{month:02d}"
    return None


def _person_name(author) -> Optional[str]:
    """``author`` may be a Person dict (``name``) or a bare string."""
    if isinstance(author, dict):
        name = author.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
        return None
    if isinstance(author, str) and author.strip():
        return author.strip()
    return None


# --- Software Advice -------------------------------------------------------


def _iter_product_reviews(node: dict):
    """Per-review ``Review`` nodes on a JSON-LD product node (SA shape)."""
    review = node.get("review")
    if isinstance(review, list):
        for rev in review:
            if isinstance(rev, dict):
                yield rev


def _sa_review(rev: dict, slug: str) -> GartnerReview:
    """One SA JSON-LD ``Review`` node → GartnerReview. Never invents fields."""
    author = rev.get("author")
    name = _person_name(author)
    # SA's JSON-LD carries no job title: worksFor.name is the reviewer's
    # EMPLOYER, not a title — the reviewer-ICP join reads reviewer_title as a
    # job title, so the org name must not land there (a company literally
    # named "Sales" would false-match "VP Sales"). numberOfEmployees.value is
    # the size band (reviewer_company_size).
    works_for = author.get("worksFor") if isinstance(author, dict) else None
    reviewer_title = None
    reviewer_company_size = None
    if isinstance(works_for, dict):
        size = works_for.get("numberOfEmployees")
        if isinstance(size, dict) and size.get("value") is not None:
            reviewer_company_size = str(size["value"]).strip() or None

    body = rev.get("reviewBody")
    body = body.strip() if isinstance(body, str) and body.strip() else None

    rating = None
    rr = rev.get("reviewRating")
    if isinstance(rr, dict):
        rating = _rating_or_none(rr.get("ratingValue"))

    posted = _sa_date(rev.get("datePublished"))

    return GartnerReview(
        review_id=_review_id(slug, name, posted, body),
        product_slug=slug,
        reviewer_name=name,
        reviewer_title=reviewer_title,
        reviewer_company_size=reviewer_company_size,
        rating=rating,
        review_body=body,
        posted_at=posted,
        review_source="softwareadvice",
    )


def _extract_sa_reviews(text: str, slug: str) -> list[GartnerReview]:
    """SA product-page JSON-LD ``Review`` nodes → GartnerReview rows."""
    reviews: list[GartnerReview] = []
    for block in extract_jsonld(text):
        for node in _graph_nodes(block):
            for rev in _iter_product_reviews(node):
                reviews.append(_sa_review(rev, slug))
    return reviews


def parse_sa_product(html, *, url) -> list[GartnerReview]:
    """Extract Software Advice product-page reviews. PURE.

    ``aggregateRating`` is the page-level rollup — deliberately NOT emitted
    as a review. Returns one ``GartnerReview`` per JSON-LD ``Review`` node,
    in document order.
    """
    return _extract_sa_reviews(_to_text(html), product_slug_from_url(url))


# --- GetApp ----------------------------------------------------------------


def _ga_note_reviews(node: dict, slug: str) -> list[GartnerReview]:
    """``positiveNotes``/``negativeNotes`` ItemLists → GartnerReview rows.

    Positive items land in ``pros`` (+ body), negative in ``cons`` (+ body).
    Per-note ratings/dates do not exist in the live JSON-LD (spike) — those
    fields stay None; an author-less note keeps reviewer_name None.
    """
    out: list[GartnerReview] = []
    for key, is_pros in (("positiveNotes", True), ("negativeNotes", False)):
        container = node.get(key)
        if not isinstance(container, dict):
            continue
        items = container.get("itemListElement")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            text = item.get("name")
            text = text.strip() if isinstance(text, str) and text.strip() else None
            if not text:
                continue
            name = _person_name(item.get("author"))
            out.append(
                GartnerReview(
                    review_id=_review_id(slug, name, None, text),
                    product_slug=slug,
                    reviewer_name=name,
                    review_body=text,
                    pros=[text] if is_pros else [],
                    cons=[] if is_pros else [text],
                    review_source="getapp",
                )
            )
    return out


def _extract_ga_reviews(text: str, slug: str) -> list[GartnerReview]:
    """GetApp product-page note-reviews → GartnerReview rows."""
    reviews: list[GartnerReview] = []
    for block in extract_jsonld(text):
        for node in _graph_nodes(block):
            has_notes = isinstance(node.get("positiveNotes"), dict) or isinstance(
                node.get("negativeNotes"), dict
            )
            if has_notes:
                reviews.extend(_ga_note_reviews(node, slug))
    return reviews


def parse_ga_product(html, *, url) -> list[GartnerReview]:
    """Extract GetApp product-page reviews. PURE.

    ``aggregateRating`` (ratingCount in the tens of thousands) is the
    page-level rollup — deliberately NOT emitted as a review.
    """
    return _extract_ga_reviews(_to_text(html), product_slug_from_url(url))


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


class _GartnerNetworkSource(SourceAdapter):
    """Shared plan/parse/harvest body for the two Gartner-network adapters.

    Structure mirrors MarketplaceCapterraSource/MarketplaceTrustRadiusSource
    (collector.py): today comes in via ``meta['today']`` (no clock reads),
    90-day default lookback, ``intent_2nd_marketplace`` candidates. Two
    deliberate differences:

    - tier ``http`` (both hosts are SSR through the repo's curl_cffi
      fetcher per the P3 spike — the browser tier is not needed).
    - the product URL rides in account ``extra_data`` (``sa_url`` /
      ``getapp_url`` — the BBB ``bbb_url`` precedent: profile URLs embed
      the site-internal product id and cannot be derived from domain+name),
      so ``requires`` stays empty and ``plan()`` gates on the seeded URL.
    """

    key: str
    tier = "http"
    cadence_hours = 168
    requires: tuple[str, ...] = ()
    _url_key: str
    _natural_prefix: str

    def _extract(self, body: str, slug: str) -> list[GartnerReview]:
        raise NotImplementedError

    def _account_url(self, account: Account) -> Optional[str]:
        extra = getattr(account, "extra_data", None) or {}
        url = extra.get(self._url_key)
        if not url:
            return None
        return str(url).strip() or None

    def _slug(self, account: Account, task_meta: dict) -> str:
        """task_meta's product_slug first; else re-derive from the account URL."""
        slug = (task_meta or {}).get("product_slug")
        if slug:
            return str(slug)
        return product_slug_from_url(self._account_url(account))

    def plan(self, account: Account, cursor: Optional[str]) -> list[FetchTask]:
        """One FetchTask per seeded product URL; nothing without it."""
        url = self._account_url(account)
        if not url:
            return []
        return [
            FetchTask(
                source=self.key,
                url=url,
                domain=account.domain,
                meta={
                    "kind": "reviews",
                    "product_slug": product_slug_from_url(url),
                    "page": 1,
                    "review_lookback_days": 90,
                },
            )
        ]

    @staticmethod
    def _observed_at(posted: Optional[str], today_str: str) -> str:
        """Full ISO day dates stand as observed_at; month-precision
        ("2026-07") and missing dates fall back to today — decay math
        downstream must never receive a partial date."""
        if posted:
            try:
                date.fromisoformat(posted)
            except (ValueError, TypeError):
                return today_str
            return posted
        return today_str

    def parse(self, doc: Document, account: Account, task_meta: dict) -> list[SignalCandidate]:
        body = (doc.body or b"").decode("utf-8", "replace")
        slug = self._slug(account, task_meta)
        reviews = self._extract(body, slug)
        if not reviews:
            return []
        today_str = (task_meta or {}).get("today", "")
        if not today_str:
            return []
        today = date.fromisoformat(today_str)
        lookback = int((task_meta or {}).get("review_lookback_days", 90))
        out: list[SignalCandidate] = []
        for r in reviews:
            posted = r.posted_at
            if posted:
                try:
                    if (today - date.fromisoformat(posted)).days > lookback:
                        continue
                except (ValueError, TypeError):
                    # Month-precision dates ("2026-07") are not ISO days —
                    # unparseable for decay math, so the review passes
                    # (family tolerance: never drop on unknown precision).
                    pass
            out.append(
                SignalCandidate(
                    signal_type="intent_2nd_marketplace",
                    observed_at=self._observed_at(r.posted_at, today_str),
                    natural_key=f"{self._natural_prefix}:{r.product_slug}:{r.review_id}",
                    title=r.review_title
                    or (
                        f"Review by {r.reviewer_name}"
                        if r.reviewer_name
                        else "Marketplace review"
                    ),
                    summary=(r.review_body or "")[:200],
                    url=r.review_url,
                    confidence=0.85 if r.verified_reviewer else 0.75,
                    evidence_data={
                        "product_slug": r.product_slug,
                        "reviewer_name": r.reviewer_name,
                        "reviewer_title": r.reviewer_title,
                        "rating": r.rating,
                        "pros": r.pros,
                        "cons": r.cons,
                        "review_source": r.review_source,
                    },
                )
            )
        return out

    def harvest_reviews(self, doc: Document, account: Account, task_meta: dict) -> list:
        """Attribute objects for the runner's ``upsert_g2_reviews`` else-branch."""
        body = (doc.body or b"").decode("utf-8", "replace")
        return self._extract(body, self._slug(account, task_meta))


@register
class MarketplaceSoftwareAdviceSource(_GartnerNetworkSource):
    """softwareadvice.com product profiles — ``extra_data['sa_url']`` opt-in."""

    key = "marketplace_softwareadvice"
    tier = "http"  # SSR via the repo's curl_cffi fetcher (P3 spike: GO)
    cadence_hours = 168
    requires: tuple[str, ...] = ()  # URL rides in extra_data (BBB precedent)
    _url_key = "sa_url"
    _natural_prefix = "sarev"

    def _extract(self, body: str, slug: str) -> list[GartnerReview]:
        return _extract_sa_reviews(body, slug)


@register
class MarketplaceGetAppSource(_GartnerNetworkSource):
    """getapp.com product profiles — ``extra_data['getapp_url']`` opt-in."""

    key = "marketplace_getapp"
    tier = "http"  # SSR via the repo's curl_cffi fetcher (P3 spike: GO)
    cadence_hours = 168
    requires: tuple[str, ...] = ()  # URL rides in extra_data (BBB precedent)
    _url_key = "getapp_url"
    _natural_prefix = "garev"

    def _extract(self, body: str, slug: str) -> list[GartnerReview]:
        return _extract_ga_reviews(body, slug)
