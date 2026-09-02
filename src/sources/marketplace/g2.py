"""Pure G2 review parser. Stdlib only — no I/O, no clock, no network.

Parses G2 product review pages (the kind served at
``https://www.g2.com/products/<slug>/reviews``) into a list of
``G2Review`` dataclasses. The parser is deliberately pure: it accepts an
already-fetched HTML string and a URL, returns structured data, and touches
nothing else.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Optional

from src.sources.marketplace.base import coerce_rating
from src.sources.marketplace.deep_reviews import normalize_bound, should_expand


_SLUG_RE = re.compile(r"/products/([^/]+)/reviews")
_STARS_RE = re.compile(r"stars-(\d+)")

_G2_BASE = "https://www.g2.com/products/{slug}/reviews"
_G2_FRAGMENT_BASE = "https://www.g2.com/products/{slug}/reviews_and_filters"


def _g2_build_url(base: str, slug: str, *, page=None, sort=None) -> str:
    """Build a G2 reviews URL from a template and optional query params.

    Plain ``?page=N`` and ``?sort=...`` query params only — G2 has no
    Next.js ``/_next/data`` route and no buildId, so pagination and sorting
    are simple query strings appended to the base URL. ``page`` is emitted
    before ``sort`` when both are given; both are omitted when ``None``.
    """
    params = []
    if page is not None:
        params.append(f"page={page}")
    if sort is not None:
        params.append(f"sort={sort}")
    url = base.format(slug=slug)
    if params:
        url = url + "?" + "&".join(params)
    return url


def g2_reviews_url(slug: str, *, page=None, sort=None) -> str:
    """Build the full G2 reviews page URL for a product slug.

    ``g2_reviews_url("sierra")`` -> ``https://www.g2.com/products/sierra/reviews``
    ``g2_reviews_url("sierra", page=2, sort="newest")`` ->
    ``https://www.g2.com/products/sierra/reviews?page=2&sort=newest``

    Pure and stdlib-only.
    """
    return _g2_build_url(_G2_BASE, slug, page=page, sort=sort)


def g2_reviews_fragment_url(slug: str, *, page=None, sort=None) -> str:
    """Build the G2 ``reviews_and_filters`` fragment URL for a product slug.

    Same plain ``?page=N`` / ``?sort=...`` query params as the full reviews
    URL, on the ``reviews_and_filters`` endpoint that serves the review DOM.

    Pure and stdlib-only.
    """
    return _g2_build_url(_G2_FRAGMENT_BASE, slug, page=page, sort=sort)


def _extract_slug(url: str) -> str:
    """Pull the product slug out of a G2 reviews URL.

    ``https://www.g2.com/products/slack/reviews`` -> ``"slack"``.
    Falls back to ``"unknown"`` when the URL does not match so callers never
    get ``None`` for a required field.
    """
    m = _SLUG_RE.search(url or "")
    return m.group(1) if m else "unknown"


def _parse_rating(stars_class: str) -> Optional[float]:
    """Convert a ``stars-N`` class string into a 0–5 float rating.

    G2 encodes the rating as an integer out of 10 (``stars-9`` == 4.5/5),
    so we divide by 2. Returns ``None`` when no number is present.
    """
    if not stars_class:
        return None
    m = _STARS_RE.search(stars_class)
    if not m:
        return None
    return int(m.group(1)) / 2.0


@dataclass
class G2Review:
    review_id: str
    product_slug: str
    reviewer_name: Optional[str] = None
    reviewer_title: Optional[str] = None
    reviewer_company_size: Optional[str] = None
    rating: Optional[float] = None
    review_title: Optional[str] = None
    review_body: Optional[str] = None
    pros: list[str] = field(default_factory=list)
    cons: list[str] = field(default_factory=list)
    posted_at: Optional[str] = None
    review_url: Optional[str] = None
    verified_reviewer: bool = False
    review_source: Optional[str] = None
    # Present in the dataclass contract, but G2's current rendered review cards
    # carry no identifiable per-card NPS-score or helpful-vote markup (verified
    # against live captures), so extraction yields None until real markup maps.
    nps_score: Optional[int] = None
    helpful_votes: Optional[int] = None


def filter_deep_reviews(reviews: list, bound) -> list:
    """Apply the deep-reviews bound to parsed G2 reviews. Pure.

    Wiring note: G2's Show More / next-page expansion is page-count-based
    (``follow_tasks`` follows pages while reviews exist), not per-review — so
    the bound is enforced at the review-filter level in the G2 adapter
    (``MarketplaceG2Source.parse``) instead of a per-review click decision.
    ``normalize_bound`` maps the config value; ``bound=None`` (explicit
    ``deep_reviews_bound: null``) keeps every review (legacy behavior).
    """
    b = normalize_bound(bound)
    if b is None:
        return list(reviews)
    return [r for r in reviews if should_expand(r.rating, b)]


def parse_g2_reviews(html: str, url: str) -> list[G2Review]:
    """Parse G2 reviews HTML into a list of G2Review. Pure."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    slug = _extract_slug(url)
    reviews = []
    cards = soup.select("div.paper[itemtype]") or soup.select("div.review-item")
    if not cards:
        cards = soup.select("[itemtype='http://schema.org/Review']") or soup.select("[itemscope]")
    for card in cards:
        author = card.select_one("[itemprop='author']")
        reviewer_name = None
        if author:
            reviewer_name = author.get_text(strip=True).split(" Information")[0]
        name_el = card.select_one("[itemprop='name']")
        review_title = name_el.get_text(strip=True) if name_el else None
        body_el = card.select_one("[itemprop='reviewBody']")
        review_body = body_el.get_text(strip=True) if body_el else None
        stars = card.select_one("div.stars")
        rating = _parse_rating(" ".join(stars.get("class", []))) if stars else None
        time_tag = card.select_one("time[datetime]")
        posted_at = time_tag["datetime"] if time_tag and time_tag.get("datetime") else None
        mt4 = card.select("div.mt-4th")
        reviewer_title = mt4[0].get_text(strip=True) if len(mt4) >= 1 else None
        reviewer_company_size = mt4[1].get_text(strip=True) if len(mt4) >= 2 else None
        pros_container = card.select_one("div[aria-label='Pros']")
        pros = [d.get_text(strip=True) for d in pros_container.select("div.ellipsis")] if pros_container else []
        cons_container = card.select_one("div[aria-label='Cons']")
        cons = [d.get_text(strip=True) for d in cons_container.select("div.ellipsis")] if cons_container else []
        card_text = card.get_text()
        verified = "Verified Reviewer" in card_text or "Verified Current User" in card_text
        review_source = None
        for span in card.select("span"):
            t = span.get_text(strip=True)
            if t.startswith("Review source:"):
                review_source = t[len("Review source:"):].strip()
        rid = hashlib.sha256(f"{slug}|{reviewer_name}|{posted_at}".encode()).hexdigest()[:16]
        reviews.append(G2Review(
            review_id=rid, product_slug=slug,
            reviewer_name=reviewer_name, reviewer_title=reviewer_title,
            reviewer_company_size=reviewer_company_size, rating=rating,
            review_title=review_title, review_body=review_body,
            pros=pros, cons=cons, posted_at=posted_at,
            review_url=url, verified_reviewer=verified, review_source=review_source,
        ))
    return reviews


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RATING_RE = re.compile(r"([\d.]+)\s*/\s*5")
_REVIEW_ID_RE = re.compile(r"(?:^|-)(\d{4,})$")
_SPHT_TEXT = "Review collected by and hosted on G2.com."
_VERIFIED_TOKENS = ("validated reviewer", "verified current user", "verified user", "verified reviewer")


def _clean_text(s: str) -> str:
    """Normalize whitespace and drop the G2 attribution boilerplate."""
    if not s:
        return ""
    s = s.replace(_SPHT_TEXT, "").replace("  ", " ").replace("  ", " ")
    return " ".join(s.split()).strip()


def extract_g2_reviews(html: str, product_slug: str) -> list[G2Review]:
    """Extract reviews from G2's current elv-* rendered review DOM. Pure.

    Parses the client-rendered review cards (``<article id="{slug}-review-123">``)
    using a deferred BeautifulSoup import. Every ``G2Review`` field is mapped
    from the live structure; no clock reads, no file I/O, no network.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    reviews = []
    cards = soup.select('article[id*="-review-"]')
    if not cards:
        cards = soup.select('article[ue="track-in-viewport"]')

    for card in cards:
        card_id = card.get("id") or ""

        # --- reviewer identity ------------------------------------------------
        name_el = card.select_one("div.elv-font-bold")
        reviewer_name = name_el.get_text(strip=True) if name_el else None

        xs = card.select("div.elv-text-xs")
        reviewer_title = xs[0].get_text(strip=True) if len(xs) >= 1 else None
        reviewer_company_size = xs[1].get_text(strip=True) if len(xs) >= 2 else None

        # --- dates / rating ----------------------------------------------------
        posted_at = None
        for meta in card.find_all("meta"):
            content = (meta.get("content") or "").strip()
            if _ISO_DATE_RE.match(content):
                posted_at = content
                break

        rating = None
        rating_el = card.select_one(".elv-star-wrapper__desc__rating")
        if rating_el:
            m = _RATING_RE.search(rating_el.get_text(" ", strip=True))
            if m:
                rating = float(m.group(1))
        if rating is None:
            stars = card.select_one(".elv-stars")
            if stars:
                rating = _parse_rating(" ".join(stars.get("class", [])))

        # Base-normalized rating coercion at the extraction edge: any
        # rating-shaped value becomes a float or 0.0; never raises.
        rating = coerce_rating(rating)

        # --- title -------------------------------------------------------------
        title_el = card.select_one("div.elv-text-lg.elv-font-bold") or card.select_one("div.elv-text-lg")
        review_title = title_el.get_text(strip=True) if title_el else None
        if review_title:
            review_title = review_title.strip('"').strip()

        # --- pros / cons / body -------------------------------------------------
        pros: list[str] = []
        cons: list[str] = []
        review_body: Optional[str] = None
        for heading in card.select("div.elv-font-bold"):
            heading_text = heading.get_text(" ", strip=True)
            if heading_text.startswith("What do you like best"):
                p = heading.find_next_sibling("p")
                if p:
                    text = _clean_text(p.get_text(" ", strip=True))
                    if text and text not in pros:
                        pros.append(text)
                        if review_body is None:
                            review_body = text
            elif heading_text.startswith("What do you dislike"):
                p = heading.find_next_sibling("p")
                if p:
                    text = _clean_text(p.get_text(" ", strip=True))
                    if text and text not in cons:
                        cons.append(text)

        # --- review URL / id ----------------------------------------------------
        review_url = None
        clip = card.select_one("[data-clipboard-text]")
        if clip and clip.get("data-clipboard-text"):
            review_url = clip["data-clipboard-text"]
        if not review_url:
            review_url = f"https://www.g2.com/products/{product_slug}/reviews"

        m = _REVIEW_ID_RE.search(card_id)
        review_id = m.group(1) if m else card_id
        if not review_id:
            review_id = hashlib.sha256(
                f"{product_slug}|{reviewer_name}|{posted_at}".encode()
            ).hexdigest()[:16]

        # --- verification / source ----------------------------------------------
        card_text = card.get_text(" ", strip=True).lower()
        verified = any(tok in card_text for tok in _VERIFIED_TOKENS)

        review_source = None
        for label in card.select(".elv-status-badge__label"):
            t = label.get_text(strip=True).strip()
            if not t:
                continue
            low = t.lower()
            if low.startswith("source: "):
                review_source = t[len("source: "):].strip()
                break
            if low == "incentivized review" or low.startswith("incentivized "):
                review_source = t
                break
            if low.startswith("review source: "):
                review_source = t[len("review source: "):].strip()
                break

        reviews.append(G2Review(
            review_id=review_id,
            product_slug=product_slug,
            reviewer_name=reviewer_name,
            reviewer_title=reviewer_title,
            reviewer_company_size=reviewer_company_size,
            rating=rating,
            review_title=review_title,
            review_body=review_body,
            pros=pros,
            cons=cons,
            posted_at=posted_at,
            review_url=review_url,
            verified_reviewer=verified,
            review_source=review_source,
            # No identifiable per-card NPS/helpful markup in current G2 DOM
            # (Step 0 discovery, Aug 2026) — None until real selectors exist.
            nps_score=None,
            helpful_votes=None,
        ))
    return reviews
