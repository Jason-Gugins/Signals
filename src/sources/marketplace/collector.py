from __future__ import annotations

import re
from datetime import date
from typing import Optional

from src.core.models import Account, Document
import json

from src.sources.base import FetchTask, SignalCandidate, SourceAdapter
from src.sources.registry import register

# Marker for G2's CURRENT client-rendered review DOM (elv-* component classes +
# `article id="{slug}-review-<digits>"` cards). The legacy fixture uses itemprop
# microdata instead, so parse()/harvest_reviews()/follow_tasks() format-detect
# the body and route to the matching pure parser.
_ELV_DOM_RE = re.compile(r"elv-stars|five-star-rater|id=[\"']?[^\"'>]*-review-\d")


def split_slugs(slug_field: str | None) -> list[str]:
    """Shared marketplace-slug splitter: comma-separated -> clean slug list."""
    if not slug_field:
        return []
    return [s.strip() for s in slug_field.split(",") if s.strip()]


def _parse_g2_body(body: str, url: str, product_slug: str) -> list:
    """Parse G2 review HTML, auto-detecting the current elv-* DOM vs legacy HTML.

    The live reviews_and_filters fragment serves reviews as client-rendered
    elv-* DOM (handled by ``extract_g2_reviews``); the legacy frozen fixture
    uses itemprop microdata (handled by ``parse_g2_reviews``). Both are kept so
    the adapter stays green against live G2 and the historical fixture.
    """
    from src.sources.marketplace.g2 import extract_g2_reviews, parse_g2_reviews

    if _ELV_DOM_RE.search(body):
        return extract_g2_reviews(body, product_slug)
    return parse_g2_reviews(body, url)


def upsert_g2_reviews(db, reviews: list, *, now: str, raw_ref: str | None = None) -> tuple[int, int]:
    """Upsert G2Review objects into the g2_reviews table. Returns (new, updated)."""
    new, updated = 0, 0
    for r in reviews:
        existing = db.one("SELECT first_seen_at FROM g2_reviews WHERE review_id=?", (r.review_id,))
        db.upsert(
            "g2_reviews",
            {
                "review_id": r.review_id,
                "product_slug": r.product_slug,
                "reviewer_name": r.reviewer_name,
                "reviewer_title": r.reviewer_title,
                "reviewer_company_size": r.reviewer_company_size,
                "rating": r.rating,
                "review_title": r.review_title,
                "review_body": r.review_body,
                "pros": json.dumps(r.pros) if r.pros else None,
                "cons": json.dumps(r.cons) if r.cons else None,
                "posted_at": r.posted_at,
                "review_url": r.review_url,
                "verified_reviewer": int(r.verified_reviewer),
                "review_source": r.review_source,
                "nps_score": r.nps_score,
                "helpful_votes": r.helpful_votes,
                "first_seen_at": existing["first_seen_at"] if existing else now,
                "last_seen_at": now,
                "raw_ref": raw_ref,
            },
            pk=("review_id",),
            overwrite={"last_seen_at", "review_body", "pros", "cons", "rating",
                        "reviewer_title", "reviewer_company_size", "raw_ref"},
        )
        if existing:
            updated += 1
        else:
            new += 1
    return new, updated


@register
class MarketplaceG2Source(SourceAdapter):
    key = "marketplace_g2"
    tier = "browser"
    cadence_hours = 168
    requires = ("g2_slug",)

    def __init__(self):
        self._session_cookie_file: str | None = None

    def _load_cookies(self) -> list[dict]:
        """Load session cookies from a JSON file if configured."""
        import json
        from pathlib import Path
        if not self._session_cookie_file:
            return []
        try:
            p = Path(self._session_cookie_file)
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
        return []

    @staticmethod
    def _split_slugs(g2_slug: str | None) -> list[str]:
        """g2_slug may hold comma-separated slugs. Returns clean slug list."""
        return split_slugs(g2_slug)

    def plan(self, account: Account, cursor: Optional[str]) -> list[FetchTask]:
        slugs = self._split_slugs(account.g2_slug)
        if not slugs:
            return []
        headers = {}
        cookies = self._load_cookies()
        if cookies:
            cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
            headers["Cookie"] = cookie_str
        return [
            FetchTask(
                source=self.key,
                url=f"https://www.g2.com/products/{slug}/reviews",
                domain=account.domain,
                headers=headers,
                meta={"kind": "reviews", "product_slug": slug, "page": 1},
            )
            for slug in slugs
        ]

    def parse(self, doc: Document, account: Account, task_meta: dict) -> list[SignalCandidate]:
        body = (doc.body or b"").decode("utf-8", "replace")
        product_slug = (task_meta or {}).get("product_slug") or account.g2_slug
        reviews = _parse_g2_body(body, doc.url or "", product_slug)
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
                    pass
            out.append(
                SignalCandidate(
                    signal_type="intent_2nd_marketplace",
                    observed_at=posted or today_str,
                    natural_key=f"g2rev:{r.product_slug}:{r.review_id}",
                    title=r.review_title or f"Review by {r.reviewer_name}",
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
        body = (doc.body or b"").decode("utf-8", "replace")
        product_slug = (task_meta or {}).get("product_slug") or account.g2_slug
        return _parse_g2_body(body, doc.url or "", product_slug)

    def follow_tasks(self, doc: Document, account: Account, task_meta: dict) -> list[FetchTask]:
        """Plan the next review page if current page had reviews and we haven't hit max_review_pages."""
        body = (doc.body or b"").decode("utf-8", "replace")
        product_slug = (task_meta or {}).get("product_slug") or account.g2_slug
        reviews = _parse_g2_body(body, doc.url or "", product_slug)
        if not reviews:
            return []
        meta = task_meta or {}
        current_page = int(meta.get("page", 1))
        max_pages = int(meta.get("max_review_pages", 5))
        if current_page >= max_pages:
            return []
        next_page = current_page + 1
        slug = meta.get("product_slug") or account.g2_slug
        if not slug:
            return []
        from src.sources.marketplace.g2 import g2_reviews_url

        url = g2_reviews_url(slug, page=next_page)
        return [
            FetchTask(
                source=self.key,
                url=url,
                domain=account.domain,
                meta={"kind": "reviews", "product_slug": slug, "page": next_page},
            )
        ]


def _parse_capterra_body(body: str, product_slug: str) -> list:
    """Parse Capterra review HTML via the pure parser (deferred bs4 import)."""
    from src.sources.marketplace.capterra import extract_capterra_reviews

    return extract_capterra_reviews(body, product_slug)


@register
class MarketplaceCapterraSource(SourceAdapter):
    key = "marketplace_capterra"
    tier = "browser"
    cadence_hours = 168
    requires = ("g2_slug",)

    def __init__(self):
        self._session_cookie_file: str | None = None

    def _load_cookies(self) -> list[dict]:
        """Load session cookies from a JSON file if configured."""
        from pathlib import Path
        if not self._session_cookie_file:
            return []
        try:
            p = Path(self._session_cookie_file)
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
        return []

    @staticmethod
    def _split_slugs(slug_field: str | None) -> list[str]:
        """Slug field may hold comma-separated '<id>/<Slug>' segments."""
        return split_slugs(slug_field)

    def plan(self, account: Account, cursor: Optional[str]) -> list[FetchTask]:
        segments = self._split_slugs(account.g2_slug)
        if not segments:
            return []
        headers = {}
        cookies = self._load_cookies()
        if cookies:
            cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
            headers["Cookie"] = cookie_str
        return [
            FetchTask(
                source=self.key,
                url=f"https://www.capterra.com/p/{segment}/reviews/",
                domain=account.domain,
                headers=headers,
                meta={
                    "kind": "reviews",
                    "product_slug": segment,
                    "page": 1,
                    "review_lookback_days": 90,
                    "max_review_pages": 3,
                },
            )
            for segment in segments
        ]

    def parse(self, doc: Document, account: Account, task_meta: dict) -> list[SignalCandidate]:
        body = (doc.body or b"").decode("utf-8", "replace")
        product_slug = (task_meta or {}).get("product_slug") or account.g2_slug
        reviews = _parse_capterra_body(body, product_slug)
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
                    pass
            out.append(
                SignalCandidate(
                    signal_type="intent_2nd_marketplace",
                    observed_at=posted or today_str,
                    natural_key=f"caprev:{r.product_slug}:{doc.url or ''}:{r.posted_at}",
                    title=r.review_title or f"Review by {r.reviewer_name}",
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
        body = (doc.body or b"").decode("utf-8", "replace")
        product_slug = (task_meta or {}).get("product_slug") or account.g2_slug
        return _parse_capterra_body(body, product_slug)

    def follow_tasks(self, doc: Document, account: Account, task_meta: dict) -> list[FetchTask]:
        """Plan the next review page if current page had reviews and we haven't hit max_review_pages."""
        body = (doc.body or b"").decode("utf-8", "replace")
        product_slug = (task_meta or {}).get("product_slug") or account.g2_slug
        reviews = _parse_capterra_body(body, product_slug)
        if not reviews:
            return []
        meta = task_meta or {}
        current_page = int(meta.get("page", 1))
        max_pages = int(meta.get("max_review_pages", 3))
        if current_page >= max_pages:
            return []
        next_page = current_page + 1
        segment = meta.get("product_slug") or account.g2_slug
        if not segment:
            return []
        return [
            FetchTask(
                source=self.key,
                url=f"https://www.capterra.com/p/{segment}/reviews/?page={next_page}",
                domain=account.domain,
                meta={"kind": "reviews", "product_slug": segment, "page": next_page},
            )
        ]
