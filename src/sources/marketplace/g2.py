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
from html.parser import HTMLParser
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


def _attr(attrs, name: str) -> Optional[str]:
    for k, v in attrs:
        if k == name:
            return v
    return None


def _class_list(cls: Optional[str]) -> list[str]:
    return (cls or "").split()


def _clean(text: str) -> str:
    """Collapse internal whitespace and strip edges."""
    return " ".join(text.split())


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


class _G2ReviewParser(HTMLParser):
    """Stateful HTMLParser that walks a G2 reviews page.

    The page is a flat list of review "cards" (``<div class="paper" ...>``),
    each containing schema.org/Review microdata. We track one card at a time
    and accumulate fields as we stream through the tags.
    """

    # itemprop values we capture as plain text.
    _ITEMPROPS = {"author", "name", "reviewBody"}

    def __init__(self, url: str):
        super().__init__()
        self.url = url
        self.slug = _extract_slug(url)
        self.reviews: list[G2Review] = []

        # Card-level state.
        self._depth = 0  # nesting depth inside the current card
        self._cur: Optional[G2Review] = None
        self._mt4th: list[str] = []  # ordered reviewer_title / company_size

        # Text-capture state. At most one of these is "active" at a time.
        self._capture: Optional[str] = None  # "author" | "name" | "reviewBody"
        self._author_tag: Optional[str] = None  # "span" or "div"
        self._buf: list[str] = []
        self._capture_depth: int = 0

        # Stars / time are handled on start tags, no text capture.
        # Pros / cons ellipsis capture.
        self._in_pros = False
        self._in_cons = False
        self._in_ellipsis = False
        self._ellipsis_buf: list[str] = []

    # -- card boundaries ------------------------------------------------
    def _is_card_start(self, tag: str, attrs) -> bool:
        if tag != "div":
            return False
        cls = _attr(attrs, "class") or ""
        classes = _class_list(cls)
        return "paper" in classes or "review-item" in classes

    def _start_card(self, attrs) -> None:
        self._cur = G2Review(review_id="", product_slug=self.slug, review_url=self.url)
        self._depth = 1
        self._mt4th = []

    def _end_card(self) -> None:
        """Finalize the current review and append it."""
        c = self._cur
        self._cur = None
        self._depth = 0
        if c is None:
            return
        # reviewer title / company size from the ordered mt-4th divs.
        if len(self._mt4th) >= 1:
            c.reviewer_title = self._mt4th[0]
        if len(self._mt4th) >= 2:
            c.reviewer_company_size = self._mt4th[1]
        # review_id from slug | reviewer_name | posted_at
        name = c.reviewer_name or ""
        posted = c.posted_at or ""
        raw = f"{c.product_slug}|{name}|{posted}".encode()
        c.review_id = hashlib.sha256(raw).hexdigest()[:16]
        self.reviews.append(c)

    # -- HTMLParser hooks ----------------------------------------------
    def handle_starttag(self, tag, attrs):
        cls = _attr(attrs, "class") or ""
        itemprop = _attr(attrs, "itemprop")

        # New review card?
        if self._cur is None and self._is_card_start(tag, attrs):
            self._start_card(attrs)
            return

        if self._cur is None:
            return

        # Track nesting so we know when the card's opening div closes.
        # The card's own opening div was counted by _start_card (depth=1);
        # every other tag inside it increments depth on start and we rely on
        # handle_endtag to decrement.
        self._depth += 1

        # itemprop text fields (author / name / reviewBody).
        if itemprop in self._ITEMPROPS and tag in ("span", "div", "p"):
            self._capture = itemprop
            self._author_tag = tag if itemprop == "author" else None
            self._buf = []
            self._capture_depth = 1
            return

        # stars rating.
        if tag == "div" and "stars" in cls and self._cur.rating is None:
            self._cur.rating = _parse_rating(cls)
            # self-closing-ish: still emit endtag, fine.

        # posted_at from <time datetime="...">.
        if tag == "time":
            dt = _attr(attrs, "datetime")
            if dt and not self._cur.posted_at:
                self._cur.posted_at = dt

        # pros / cons containers.
        if tag == "div":
            aria = _attr(attrs, "aria-label")
            if aria == "Pros":
                self._in_pros = True
            elif aria == "Cons":
                self._in_cons = True
            # reviewer meta: mt-4th divs.
            if "mt-4th" in _class_list(cls):
                self._capture = "__mt4th__"
                self._buf = []
                self._capture_depth = 1
                return

        # ellipsis items inside pros/cons.
        if tag == "div" and "ellipsis" in _class_list(cls) and (self._in_pros or self._in_cons):
            self._in_ellipsis = True
            self._ellipsis_buf = []
            return

    def handle_startendtag(self, tag, attrs):
        # Self-closing tags (e.g. <div ... />) — handle attributes but no body.
        # Treat like a start immediately followed by end for attribute-only
        # fields (stars, time). For text fields there is no body to capture.
        cls = _attr(attrs, "class") or ""
        itemprop = _attr(attrs, "itemprop")

        if self._cur is None:
            if self._is_card_start(tag, attrs):
                # A self-closed card div is degenerate; skip.
                return
            return

        if tag == "div" and "stars" in cls and self._cur.rating is None:
            self._cur.rating = _parse_rating(cls)
        if tag == "time":
            dt = _attr(attrs, "datetime")
            if dt and not self._cur.posted_at:
                self._cur.posted_at = dt
        if tag == "div":
            aria = _attr(attrs, "aria-label")
            if aria == "Pros":
                self._in_pros = True
            elif aria == "Cons":
                self._in_cons = True
        if tag == "div" and "ellipsis" in _class_list(cls) and (self._in_pros or self._in_cons):
            # self-closed ellipsis has no text — ignore.
            pass
        # No depth bookkeeping needed for startend (no matching endtag).

    def handle_data(self, data):
        if self._cur is None:
            return
        # Text capture for itemprop / mt-4th fields.
        if self._capture is not None and self._capture != "__mt4th__" and self._capture != "__mt4th__":
            self._buf.append(data)
            return
        if self._capture == "__mt4th__":
            self._buf.append(data)
            return
        # ellipsis item text.
        if self._in_ellipsis:
            self._ellipsis_buf.append(data)
            return
        # Loose text inside the card: check for verified / source markers.
        # These spans have no itemprop, so we inspect raw text.
        stripped = data.strip()
        if stripped:
            if stripped in ("Verified Reviewer", "Verified Current User"):
                self._cur.verified_reviewer = True
            elif stripped.startswith("Review source:"):
                self._cur.review_source = stripped[len("Review source:"):].strip()

    def handle_endtag(self, tag):
        if self._cur is None:
            return

        # Finish an ellipsis item.
        if self._in_ellipsis and tag == "div":
            text = _clean("".join(self._ellipsis_buf))
            if text:
                if self._in_pros:
                    self._cur.pros.append(text)
                elif self._in_cons:
                    self._cur.cons.append(text)
            self._in_ellipsis = False
            self._ellipsis_buf = []
            self._depth -= 1
            return

        # Close a pros/cons container.
        if tag == "div" and self._in_pros:
            self._in_pros = False
            self._depth -= 1
            return
        if tag == "div" and self._in_cons:
            self._in_cons = False
            self._depth -= 1
            return

        # Finish an itemprop text capture.
        if self._capture is not None and tag in ("span", "div", "p"):
            text = _clean("".join(self._buf))
            if self._capture == "author":
                # If author came from a div, strip trailing " Information" noise.
                if self._author_tag == "div" and " Information" in text:
                    text = text.split(" Information")[0]
                self._cur.reviewer_name = text
            elif self._capture == "name":
                self._cur.review_title = text
            elif self._capture == "reviewBody":
                self._cur.review_body = text
            elif self._capture == "__mt4th__":
                if text:
                    self._mt4th.append(text)
            self._capture = None
            self._author_tag = None
            self._buf = []
            self._capture_depth = 0
            self._depth -= 1
            return

        # Closing the card's opening div ends the card.
        self._depth -= 1
        if self._depth <= 0:
            self._end_card()


def parse_g2_reviews(html: str, url: str) -> list[G2Review]:
    """Parse a G2 reviews HTML page into a list of ``G2Review``.

    Pure: reads ``html`` and ``url`` only; performs no I/O.
    """
    p = _G2ReviewParser(url)
    p.feed(html)
    p.close()
    return p.reviews
