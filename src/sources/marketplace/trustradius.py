"""Marketplace review parsers. PURE. Browser tier opt-in."""

from __future__ import annotations

from dataclasses import dataclass
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
