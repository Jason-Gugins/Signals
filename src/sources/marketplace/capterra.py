"""Pure Capterra review parser. Stdlib + deferred bs4 — no I/O, no clock, no network.

Parses Capterra product review pages (``https://www.capterra.com/p/<id>/<Slug>/reviews/``)
into a list of ``CapterraReview`` dataclasses. Capterra reviews pages are
SERVER-RENDERED (Next.js RSC): all review cards are present in the raw HTML,
so this pure parser works directly on the fetched document — no fragment
fetch needed.

Parsing is by in-card semantics/test-ids only:

- Card container: ``div[data-test-id='review-cards-container']`` (hyphenated
  ``data-test-id``; the page also uses camelCase ``data-testid`` elsewhere —
  BOTH spellings exist, neither is assumed for the other).
- Overall rating: ``div[data-testid='Overall Rating-rating']`` — numeric span
  text ("5.0") preferred, star-full count (``i[data-rating]``) as fallback.
- Pros/Cons: spans labeled 'Pros'/'Cons' followed by sibling ``<p>`` text.
- Date: ``div.typo-0.text-neutral-90`` month-name text ("July 11, 2026").

NOTE: never select by Emotion hash classes (``c1ofrhif``, ``sr2r3oj``, ...) —
they are build-specific. Hash classes are only ever read as opaque values,
never used as selectors.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Optional

from src.core.textutil import to_iso_date
# Keep the TrustRadius-era parser importable from this module for backward
# compatibility: tests/test_marketplace.py imports parse_capterra_reviews from
# src.sources.marketplace.capterra and asserts it parses the TrustRadius
# fixture. The Capterra-specific extraction is extract_capterra_reviews below.
from src.sources.marketplace.trustradius import parse_capterra_reviews  # noqa: F401

__all__ = ["CapterraReview", "extract_capterra_reviews", "parse_capterra_reviews"]

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

_MONTH_DATE_RE = re.compile(
    r"^(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+(\d{1,2}),\s+(\d{4})$"
)


def _month_name_to_iso(text: str) -> Optional[str]:
    """'July 11, 2026' -> '2026-07-11'. Pure month-name parsing, no clock."""
    if not text:
        return None
    m = _MONTH_DATE_RE.match(text.strip())
    if not m:
        return None
    month = _MONTHS.get(m.group(1).lower())
    if not month:
        return None
    return f"{int(m.group(3)):04d}-{month:02d}-{int(m.group(2)):02d}"


@dataclass
class CapterraReview:
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
    # Present in the dataclass contract, but Capterra's current rendered review
    # cards carry no NPS-score or helpful-vote markup (Task 1 spike, Aug 2026),
    # so extraction yields None until real markup maps.
    nps_score: Optional[int] = None
    helpful_votes: Optional[int] = None


def _card_rating(overall_div) -> Optional[float]:
    """Rating from the 'Overall Rating-rating' div.

    Preferred: the numeric span text ("5.0") inside the rating widget.
    Fallback: count of star-full <i> elements (i[aria-label='star-full'],
    each carrying data-rating). Selected by structure inside the test-id div,
    never by the Emotion hash class on the numeric span.
    """
    if overall_div is None:
        return None
    for span in overall_div.find_all("span"):
        text = span.get_text(strip=True)
        if text and text.replace(".", "", 1).isdigit():
            return float(text)
    return float(
        len(overall_div.select("i[aria-label='star-full']"))
    ) or None


def _section_texts(card, label: str) -> list[str]:
    """Texts of the <p> siblings following a 'Pros'/'Cons' label span."""
    out: list[str] = []
    for span in card.find_all("span", string=lambda t: t and t.strip() == label):
        p = span.find_next("p")
        if p is None:
            continue
        text = " ".join(p.get_text(" ", strip=True).split())
        if text:
            out.append(text)
    return out


def extract_capterra_reviews(html: str, product_slug: str) -> list[CapterraReview]:
    """Extract reviews from a rendered Capterra reviews page. Pure.

    Accepts the full server-rendered document (Next.js RSC) and returns one
    ``CapterraReview`` per review card inside
    ``div[data-test-id='review-cards-container']``. No clock reads, no file
    I/O, no network; bs4 is imported lazily inside the function.
    """
    from bs4 import BeautifulSoup

    if not html:
        return []

    soup = BeautifulSoup(html, "lxml")
    container = soup.select_one("div[data-test-id='review-cards-container']")
    if container is None:
        return []

    cards = [
        c
        for c in container.find_all(recursive=False)
        if c.select_one("div[data-testid='Overall Rating-rating']")
    ]

    reviews: list[CapterraReview] = []
    for card in cards:
        # --- rating ------------------------------------------------------
        overall_div = card.select_one("div[data-testid='Overall Rating-rating']")
        rating = _card_rating(overall_div)

        # --- date ----------------------------------------------------------
        # Normalized via _month_name_to_iso (month-name form) with a
        # to_iso_date fallback (ISO/other shapes) so the same review rendered
        # with a different date format normalizes to the same ISO day.
        posted_at = None
        date_div = card.select_one("div.typo-0.text-neutral-90")
        if date_div:
            raw_date = date_div.get_text(strip=True)
            posted_at = _month_name_to_iso(raw_date) or to_iso_date(raw_date)

        # --- reviewer identity ----------------------------------------------
        reviewer_name = None
        reviewer_title = None
        reviewer_company_size = None
        name_el = card.select_one("span.typo-20.font-semibold")
        if name_el:
            reviewer_name = name_el.get_text(strip=True)
            # Detail lines follow the name inside the same info block,
            # separated by <br>: title, industry, "Used the software for: N".
            info = name_el.find_parent("div")
            if info is not None:
                lines = [ln.strip() for ln in info.get_text("\n", strip=True).split("\n") if ln.strip()]
                after = lines[lines.index(reviewer_name) + 1:] if reviewer_name in lines else []
                if after and not after[0].lower().startswith("used the software"):
                    reviewer_title = after[0]
                for line in after:
                    low = line.lower()
                    if "employees" in low:
                        reviewer_company_size = line
                        break

        # --- title -----------------------------------------------------------
        title_el = card.select_one("h3")
        review_title = title_el.get_text(strip=True) if title_el else None
        if review_title:
            review_title = review_title.strip('"').strip()

        # --- pros / cons -----------------------------------------------------
        pros = _section_texts(card, "Pros")
        cons = _section_texts(card, "Cons")

        # --- verification / source -------------------------------------------
        card_text = card.get_text(" ", strip=True).lower()
        verified = "verified" in card_text

        review_source = None
        source_label = card.select_one("span#review-source-label") or card.select_one(
            "div[aria-labelledby='review-source-label']"
        )
        if source_label:
            dialog = source_label.find_next("div", role="dialog") or source_label.parent.find(
                "div", role="dialog"
            )
            if dialog is not None:
                text = " ".join(dialog.get_text(" ", strip=True).split())
                head = text.split(":")[0].strip()
                review_source = head or (text[:80] if text else None)

        # --- id / url -----------------------------------------------------------
        review_url = None
        for a in card.find_all("a", href=True):
            href = a["href"]
            if "/reviews" in href and "assets" not in href:
                review_url = href
                break

        # review_id inputs (pinned by tests/test_capterra_reviewid.py):
        # sha256(f"{product_slug}|{reviewer_name}|{posted_iso}")[:16] where
        # posted_iso is the ISO-normalized date, NOT the raw rendered string,
        # so re-renders with different date formats keep the same key. When
        # the date is unparseable (posted_iso is None) the RAW rendered date
        # string is used instead so distinct unknown-date reviews never
        # collapse onto the literal 'None' sentinel.
        posted_iso = to_iso_date(posted_at) if posted_at else None
        id_date = posted_iso if posted_iso is not None else (
            date_div.get_text(strip=True) if date_div else ""
        )
        review_id = hashlib.sha256(
            f"{product_slug}|{reviewer_name}|{id_date}".encode()
        ).hexdigest()[:16]

        reviews.append(
            CapterraReview(
                review_id=review_id,
                product_slug=product_slug,
                reviewer_name=reviewer_name,
                reviewer_title=reviewer_title,
                reviewer_company_size=reviewer_company_size,
                rating=rating,
                review_title=review_title,
                review_body=(pros[0] if pros else None),
                pros=pros,
                cons=cons,
                posted_at=posted_at,
                review_url=review_url,
                verified_reviewer=verified,
                review_source=review_source,
                # No per-card NPS/helpful markup in the current Capterra DOM
                # (Task 1 spike, Aug 2026) — None until real selectors exist.
                nps_score=None,
                helpful_votes=None,
            )
        )
    return reviews
