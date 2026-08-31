"""Marketplace review parsers. PURE. Browser tier opt-in.

Also hosts the modern TrustRadius review parser (``TrustRadiusReview`` +
``extract_trustradius_reviews``), mirroring the Capterra parser pattern:
pure, deferred bs4, selectors taken only from the real live capture
(``tests/fixtures/marketplace/trustradius_slack_reviews.html`` — captured
Aug 2026 via curl_cffi chrome impersonation, HTTP 200).

Real TrustRadius reviews-page DOM (from the live fixture):

- Review card: ``<article class="Review_review__5RC6b">`` — the class hash
  suffix is build-specific and NEVER selected; cards are identified
  structurally as ``article`` elements containing BOTH
  ``div[data-testid='stars-container']`` and ``div[data-testid='content']``.
- Rating: ``div[data-testid='stars-container']`` carries ``data-rating="9"``
  on a 0–10 scale (TrustRadius uses a 10-point rating) → normalized to 0–5
  by dividing by 2. Fallback: the sr-only ``Rating: 9 out of 10`` text.
- Date: ``<time datetime="2026-08-10T16:20:28.250Z">`` → ISO date prefix.
- Title/URL: ``header h2 a`` (href ``/reviews/<review-slug>``); the review
  slug tail is the stable per-review id.
- Body: ``div[data-testid='content']``.
- Pros/Cons: ``h3`` "Pros"/"Cons" followed by a ``<ul>`` of ``<li>`` spans.
- Reviewer: ``a[data-testid='byline']`` — name from the avatar fallback
  ``span[title="<Name>'s avatar"]``, job line from the div after the name;
  company size parsed from "(501-1000 employees)".
- Verification: "Vetted Review" text / ``[data-testid='vetted-icon']``.
- Pagination is client-side (no rendered ``?page`` links in the HTML);
  follow_tasks still uses the conventional ``?page=N`` query param.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from html.parser import HTMLParser
from typing import Optional

from src.core.textutil import to_iso_date
from src.identity.registry import AccountRegistry
from src.sources.base import SignalCandidate


@dataclass(frozen=True)
class Review:
    author_company: str | None
    author_title: str | None
    posted_at: str | None
    rating: float | None
    product: str
    url: str
    snippet: str | None


def _attr(attrs, name):
    for k, v in attrs:
        if k == name:
            return v
    return None


class _ReviewParser(HTMLParser):
    def __init__(self, product, url):
        super().__init__()
        self.product = product
        self.url = url
        self.reviews: list[Review] = []
        self._cap = False
        self._buf = []
        self._company = None
        self._date = None

    def handle_starttag(self, tag, attrs):
        cls = _attr(attrs, "class") or ""
        if "review" in cls and tag == "article":
            self._cap = True
            self._company = _attr(attrs, "data-company")
            self._date = _attr(attrs, "data-date")
            self._buf = []
        if tag == "time" and self._cap:
            self._date = self._date or _attr(attrs, "datetime")

    def handle_data(self, data):
        if self._cap:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "article" and self._cap:
            text = " ".join(self._buf)
            self.reviews.append(
                Review(self._company, None, to_iso_date(self._date), None, self.product, self.url, text[:200])
            )
            self._cap = False


def parse_trustradius_reviews(html: str, url: str) -> list[Review]:
    p = _ReviewParser("product", url)
    p.feed(html)
    return p.reviews


def parse_g2_reviews(html: str, url: str) -> list[Review]:
    return parse_trustradius_reviews(html, url)


def parse_capterra_reviews(html: str, url: str) -> list[Review]:
    return parse_trustradius_reviews(html, url)


def reviews_to_candidates(reviews, registry: AccountRegistry, *, today: date, own_product: str) -> list[tuple[str, SignalCandidate]]:
    out = []
    for r in reviews:
        posted = to_iso_date(r.posted_at)
        if posted and (today - date.fromisoformat(posted)).days > 90:
            continue
        acct = registry.resolve(name=r.author_company) if r.author_company else None
        if not acct:
            continue
        kind = "own" if own_product.lower() in (r.product or "").lower() or "compar" in r.url else "competitor"
        conf = 0.85 if kind != "competitor" else 0.75
        out.append(
            (
                acct.domain,
                SignalCandidate(
                    signal_type="intent_2nd_marketplace",
                    observed_at=posted or today.isoformat(),
                    natural_key=f"rev:{acct.domain}:{r.url}:{posted}",
                    title=f"Review by {r.author_company}",
                    url=r.url,
                    confidence=conf,
                    evidence_data={"product": r.product, "snippet": r.snippet},
                ),
            )
        )
    return out


# --------------------------------------------------------------------------
# Modern TrustRadius parser (mirrors extract_capterra_reviews in capterra.py)
# --------------------------------------------------------------------------

_TR_BASE = "https://www.trustradius.com"

_RATING10_RE = re.compile(r"Rating:\s*(\d+)\s*out of\s*10", re.I)
_COMPANY_SIZE_RE = re.compile(r"\(([^)]*employees)\)", re.I)


@dataclass
class TrustRadiusReview:
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
    # Present in the dataclass contract, but TrustRadius review cards carry no
    # NPS-score or helpful-vote markup (live capture, Aug 2026), so extraction
    # yields None until real markup maps.
    nps_score: Optional[int] = None
    helpful_votes: Optional[int] = None


def _card_rating(card) -> Optional[float]:
    """TrustRadius rates on a 0-10 scale; normalize to 0-5.

    Preferred: data-rating on the stars-container test id.
    Fallback: sr-only "Rating: 9 out of 10" text.
    """
    stars = card.select_one("div[data-testid='stars-container']")
    if stars is not None:
        raw = stars.get("data-rating")
        if raw:
            try:
                return round(float(raw) / 2.0, 1)
            except ValueError:
                pass
    sr = card.find("div", class_="_sr-only_")
    if sr is not None:
        m = _RATING10_RE.search(sr.get_text(strip=True))
        if m:
            return round(int(m.group(1)) / 2.0, 1)
    return None


def _section_items(card, label: str) -> list:
    """li span texts following the 'Pros'/'Cons' h3 in the review content."""
    out = []
    content = card.select_one("div[data-testid='content']")
    if content is None:
        return out
    for h3 in content.find_all("h3"):
        if h3.get_text(strip=True) != label:
            continue
        ul = h3.find_next("ul")
        if ul is None:
            continue
        for li in ul.find_all("li"):
            text = " ".join(li.get_text(" ", strip=True).split())
            if text:
                out.append(text)
    return out


def _byline_fields(card) -> tuple:
    """(reviewer_name, reviewer_title, reviewer_company_size) from the byline."""
    byline = card.select_one("a[data-testid='byline']") or card.select_one(
        "div[data-testid='byline']"
    )
    if byline is None:
        return None, None, None
    # Name: the avatar fallback span carries title="<Name>'s avatar".
    name = None
    avatar = byline.select_one("div[data-testid='avatar-container'] span[title]")
    if avatar is not None:
        title = avatar.get("title") or ""
        name = re.sub(r"'s avatar$", "", title).strip() or None
    # Job line: the div after the name inside the byline body block.
    job = None
    for div in byline.find_all("div"):
        text = " ".join(div.get_text(" ", strip=True).split())
        if text and name and text.startswith(name) and text != name:
            job = text[len(name):].strip()
            break
    company_size = None
    if job:
        m = _COMPANY_SIZE_RE.search(job)
        if m:
            company_size = m.group(1).strip()
    return name, (job or None), company_size


def extract_trustradius_reviews(html: str, product_slug: str) -> list:
    """Extract reviews from a rendered TrustRadius reviews page. Pure.

    Accepts the server-rendered document (Next.js) and returns one
    ``TrustRadiusReview`` per review card. Cards are ``article`` elements
    containing both the stars-container and content test ids. No clock
    reads, no file I/O, no network; bs4 is imported lazily inside the
    function. Never selects by the build-specific hash class suffixes.
    """
    from bs4 import BeautifulSoup

    if not html:
        return []

    soup = BeautifulSoup(html, "lxml")
    cards = [
        a
        for a in soup.find_all("article")
        if a.select_one("div[data-testid='stars-container']")
        and a.select_one("div[data-testid='content']")
    ]

    reviews = []
    for card in cards:
        # --- rating (0-10 -> 0-5) -----------------------------------------
        rating = _card_rating(card)

        # --- date ----------------------------------------------------------
        posted_at = None
        time_el = card.select_one("time[datetime]")
        if time_el is not None:
            posted_at = (time_el.get("datetime") or "")[:10] or None

        # --- title / url / id ----------------------------------------------
        review_title = None
        review_url = None
        review_id = None
        title_a = card.select_one("header h2 a[href]")
        if title_a is not None:
            review_title = title_a.get_text(strip=True).strip('"').strip()
            href = title_a["href"]
            review_url = href if href.startswith("http") else _TR_BASE + href
            review_id = href.rstrip("/").rsplit("/", 1)[-1] or None

        # --- body ------------------------------------------------------------
        body = None
        content = card.select_one("div[data-testid='content']")
        if content is not None:
            body = " ".join(content.get_text(" ", strip=True).split())

        # --- reviewer ---------------------------------------------------------
        reviewer_name, reviewer_title, reviewer_company_size = _byline_fields(card)

        # --- verification -------------------------------------------------------
        card_text = card.get_text(" ", strip=True).lower()
        verified = (
            "vetted review" in card_text
            or card.select_one("[data-testid='vetted-icon']") is not None
        )

        if not review_id:
            # Deterministic fallback id (same recipe as Capterra).
            review_id = hashlib.sha256(
                f"{product_slug}|{reviewer_name}|{posted_at}".encode()
            ).hexdigest()[:16]

        pros = _section_items(card, "Pros")
        cons = _section_items(card, "Cons")
        # A real review card always carries at least one of: rating, title,
        # date. Cards with none of the three are stale/divergent markup —
        # skip them so the self-check can flag drift instead of emitting
        # empty reviews.
        if rating is None and review_title is None and posted_at is None:
            continue
        reviews.append(
            TrustRadiusReview(
                review_id=review_id,
                product_slug=product_slug,
                reviewer_name=reviewer_name,
                reviewer_title=reviewer_title,
                reviewer_company_size=reviewer_company_size,
                rating=rating,
                review_title=review_title,
                review_body=body,
                pros=pros,
                cons=cons,
                posted_at=posted_at,
                review_url=review_url,
                verified_reviewer=verified,
                review_source=None,
                # No per-card NPS/helpful markup in the current TrustRadius
                # DOM (live capture, Aug 2026) - None until real selectors exist.
                nps_score=None,
                helpful_votes=None,
            )
        )
    return reviews
