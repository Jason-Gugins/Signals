"""Selector-drift self-check for the marketplace review parsers (G2 + Capterra).

Fetches one known-good product's reviews page and verifies the parser still
extracts reviews. States:
  ok        — >=1 review parsed
  drift     — page has review markup but 0 parsed
  empty     — page genuinely has no review cards (new/quiet product)
  challenge — anti-bot interstitial (network problem, not parser problem)
  error     — fetch failed

``source`` selects the branch:
  g2       — stealth-browser fragment URL + elv-* markers (client-rendered)
  capterra — server-rendered reviews page, ``<id>/<Slug>`` URL segment,
             container-marker drift detection. Capterra is fronted by
             Cloudflare, so challenge detection also covers CF bodies.
"""
from __future__ import annotations

from dataclasses import dataclass

_CAPTERRA_CHALLENGE_MARKERS = (b"cf-chl", b"Just a moment")


@dataclass
class SelfcheckResult:
    state: str
    review_count: int
    url: str
    detail: str = ""


def capterra_reviews_url(segment: str, *, page: int | None = None) -> str:
    """``'19319/JIRA'`` -> ``https://www.capterra.com/p/19319/JIRA/reviews/``.

    The segment is the full ``<numeric-id>/<Slug>`` path — the numeric id is
    part of Capterra's URL and cannot be derived from the slug alone.
    """
    url = f"https://www.capterra.com/p/{segment}/reviews/"
    if page is not None:
        url += f"?page={page}"
    return url


def run_selfcheck(fetcher, *, slug: str, config=None, source: str = "g2") -> SelfcheckResult:
    if source == "capterra":
        return _run_selfcheck_capterra(fetcher, slug=slug)
    return _run_selfcheck_g2(fetcher, slug=slug)


def _run_selfcheck_g2(fetcher, *, slug: str) -> SelfcheckResult:
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


def _run_selfcheck_capterra(fetcher, *, slug: str) -> SelfcheckResult:
    from src.sources.marketplace.capterra import extract_capterra_reviews
    from src.sources.techstack.datadome import is_datadome_challenge

    url = capterra_reviews_url(slug)
    try:
        # Capterra is server-rendered, so the plain fetcher is used — no
        # browser needed. The fetcher may be a CurlCffiFetcher shim or any
        # object exposing fetch(url, ...) -> FetchResult-like with .doc.
        result = fetcher.fetch(url, source="marketplace_capterra",
                               domain="capterra.com")
    except Exception as e:  # noqa: BLE001
        return SelfcheckResult("error", 0, url, detail=str(e))
    if result is None or not result.ok or result.doc is None:
        return SelfcheckResult("error", 0, url, detail="fetch returned no document")
    body = result.doc.body or b""
    if (is_datadome_challenge(status=result.status, body=body)
            or any(m in body for m in _CAPTERRA_CHALLENGE_MARKERS)):
        return SelfcheckResult("challenge", 0, url)
    html = body.decode("utf-8", "replace")
    reviews = extract_capterra_reviews(html, slug)
    if reviews:
        return SelfcheckResult("ok", len(reviews), url)
    # Drift marker: the review-cards container is present but nothing parsed.
    has_markers = (b"review-cards-container" in body)
    if has_markers:
        return SelfcheckResult("drift", 0, url,
                               detail="review-cards-container present but parser extracted 0 — selectors stale")
    return SelfcheckResult("empty", 0, url)
