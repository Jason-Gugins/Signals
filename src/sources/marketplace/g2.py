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


_SLUG_RE = re.compile(r"/products/([^/]+)/reviews")
_STARS_RE = re.compile(r"stars-(\d+)")


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
