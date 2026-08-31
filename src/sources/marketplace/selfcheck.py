"""Selector-drift self-check for the G2 elv-* parser.

Fetches one known-good product's reviews_and_filters fragment through the
stealth browser and verifies the parser still extracts reviews. States:
  ok        — >=1 review parsed
  drift     — page has review markup (elv-stars / -review- ids) but 0 parsed
  empty     — page genuinely has no review cards (new/quiet product)
  challenge — DataDome interstitial (network problem, not parser problem)
  error     — fetch failed
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SelfcheckResult:
    state: str
    review_count: int
    url: str
    detail: str = ""


def run_selfcheck(fetcher, *, slug: str, config=None) -> SelfcheckResult:
    from src.sources.marketplace.g2 import (
        extract_g2_reviews,
        g2_reviews_fragment_url,
    )
    from src.sources.techstack.datadome import is_datadome_challenge

    url = g2_reviews_fragment_url(slug, page=None)
    try:
        result = fetcher.fetch(url, source="marketplace_g2", domain="g2.com",
                               wait_ms=4000, scroll=True,
                               warmup_url="https://www.g2.com/", warmup_ms=4000)
    except Exception as e:  # noqa: BLE001 — any fetch failure is the 'error' state
        return SelfcheckResult("error", 0, url, detail=str(e))
    if result is None or not result.ok or result.doc is None:
        return SelfcheckResult("error", 0, url, detail="fetch returned no document")
    body = result.doc.body or b""
    if is_datadome_challenge(status=result.status, body=body):
        return SelfcheckResult("challenge", 0, url)
    html = body.decode("utf-8", "replace")
    reviews = extract_g2_reviews(html, slug)
    if reviews:
        return SelfcheckResult("ok", len(reviews), url)
    has_markers = (b"elv-stars" in body) or (b"-review-" in body)
    if has_markers:
        return SelfcheckResult("drift", 0, url,
                               detail="review markup present but parser extracted 0 — selectors stale")
    return SelfcheckResult("empty", 0, url)
