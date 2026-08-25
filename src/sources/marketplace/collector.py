from __future__ import annotations

from datetime import date
from typing import Optional

from src.core.models import Account, Document
from src.sources.base import FetchTask, SignalCandidate, SourceAdapter
from src.sources.registry import register


@register
class MarketplaceG2Source(SourceAdapter):
    key = "marketplace_g2"
    tier = "browser"
    cadence_hours = 168
    requires = ("g2_slug",)

    def plan(self, account: Account, cursor: Optional[str]) -> list[FetchTask]:
        if not account.g2_slug:
            return []
        url = f"https://www.g2.com/products/{account.g2_slug}/reviews"
        return [
            FetchTask(
                source=self.key,
                url=url,
                domain=account.domain,
                meta={"kind": "reviews", "product_slug": account.g2_slug},
            )
        ]

    def parse(self, doc: Document, account: Account, task_meta: dict) -> list[SignalCandidate]:
        from src.sources.marketplace.g2 import parse_g2_reviews

        body = (doc.body or b"").decode("utf-8", "replace")
        reviews = parse_g2_reviews(body, doc.url or "")
        if not reviews:
            return []
        today_str = (task_meta or {}).get("today", "")
        if not today_str:
            return []
        today = date.fromisoformat(today_str)
        out: list[SignalCandidate] = []
        for r in reviews:
            posted = r.posted_at
            if posted:
                try:
                    if (today - date.fromisoformat(posted)).days > 90:
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
        from src.sources.marketplace.g2 import parse_g2_reviews

        body = (doc.body or b"").decode("utf-8", "replace")
        return parse_g2_reviews(body, doc.url or "")
