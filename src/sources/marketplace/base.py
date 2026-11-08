"""MarketplaceReview — shared normalizing base for marketplace review rows.

Plan Task 8: a site-agnostic dataclass with a ``from_raw`` classmethod that
performs safe type coercion at the extraction edge (never raises). The
per-source dataclasses (e.g. ``G2Review``) and the ``g2_reviews`` table upsert
shape are unchanged — this base is the normalization contract only.
"""

from dataclasses import dataclass
from typing import Any


def coerce_rating(value: Any) -> float:
    """Safely coerce a rating-like value to float, defaulting to 0.0.

    Accepts str/int/float/None (or anything else); never raises.
    """
    try:
        if value is None:
            return 0.0
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0


@dataclass
class MarketplaceReview:
    """Normalized marketplace review row (site-agnostic)."""

    review_id: str
    product_slug: str
    reviewer: str | None = None
    rating: float = 0.0
    posted: str | None = None
    body: str | None = None
    source: str | None = None
    url: str | None = None
    helpful_votes: int | None = None
    nps: int | None = None

    @classmethod
    def from_raw(cls, raw: dict) -> "MarketplaceReview":
        """Build a review from a raw dict, coercing types safely.

        Rating coercion: str/int/None -> float or 0.0; never raises.
        """
        return cls(
            review_id=str(raw.get("review_id") or ""),
            product_slug=str(raw.get("product_slug") or ""),
            reviewer=raw.get("reviewer"),
            rating=coerce_rating(raw.get("rating")),
            posted=raw.get("posted"),
            body=raw.get("body"),
            source=raw.get("source"),
            url=raw.get("url"),
            helpful_votes=raw.get("helpful_votes"),
            nps=raw.get("nps"),
        )
