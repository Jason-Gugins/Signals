"""Deep-reviews bounding — pure decision helpers for "Show More" expansion.

Plan Task 9: bounding is the NEW DEFAULT. ``deep_reviews_bound`` in
``config/marketplace.yaml`` under ``sites.g2`` defaults to ``'extreme'``
(absent or invalid values normalize to ``'extreme'``), meaning deep-review
expansion only proceeds when a review's rating is extreme (>= 4.0 or <= 2.0).
Set ``deep_reviews_bound: null`` in config for the old bound-free behavior.
"""

from typing import Optional

DEFAULT_BOUND = "extreme"
_VALID_BOUNDS = {"extreme"}


def normalize_bound(value) -> Optional[str]:
    """Normalize a config bound value; absent/invalid -> 'extreme' default.

    The config key ``deep_reviews_bound`` is new: an absent or invalid value
    normalizes to ``'extreme'`` (bounding is the new default).
    """
    if value in _VALID_BOUNDS:
        return value
    return DEFAULT_BOUND


def should_expand(review_rating: Optional[float], bound: Optional[str]) -> bool:
    """Decide whether a review justifies deep-review (Show More) expansion.

    Pure. ``bound='extreme'`` expands only ratings >= 4.0 or <= 2.0;
    unknown (None) ratings expand conservatively. ``bound=None`` is the
    old bound-free behavior: always expand.
    """
    if bound == "extreme":
        if review_rating is None:
            return True
        return review_rating >= 4.0 or review_rating <= 2.0
    # bound-free (old behavior): always expand
    return True
