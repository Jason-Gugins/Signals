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

The five-state logic lives in :mod:`src.core.selfcheck`
(:func:`run_source_selfcheck`); ``_run_selfcheck_*`` are thin wrappers that
supply each source's URL, extractor, challenge detector and drift hints.
``SelfcheckResult`` moved to ``src.core.selfcheck`` and is re-exported here
so existing imports keep working.
"""
from __future__ import annotations

from src.core.selfcheck import SelfcheckResult, run_source_selfcheck  # noqa: F401

_CAPTERRA_CHALLENGE_MARKERS = (b"cf-chl", b"Just a moment")

# TrustRadius is CF-fronted too (Next.js server-rendered pages behind
# Cloudflare) — same challenge markers as Capterra.
_TRUSTRADIUS_CHALLENGE_MARKERS = _CAPTERRA_CHALLENGE_MARKERS


def capterra_reviews_url(segment: str, *, page: int | None = None) -> str:
    """``'19319/JIRA'`` -> ``https://www.capterra.com/p/19319/JIRA/reviews/``.

    The segment is the full ``<numeric-id>/<Slug>`` path — the numeric id is
    part of Capterra's URL and cannot be derived from the slug alone.
    """
    url = f"https://www.capterra.com/p/{segment}/reviews/"
    if page is not None:
        url += f"?page={page}"
    return url


def trustradius_reviews_url(slug: str, *, page: int | None = None) -> str:
    """``'slack'`` -> ``https://www.trustradius.com/products/slack/reviews``.

    Pagination is client-side on the live page, but the conventional
    ``?page=N`` query param is honored by follow_tasks.
    """
    url = f"https://www.trustradius.com/products/{slug}/reviews"
    if page is not None:
        url += f"?page={page}"
    return url


def run_selfcheck(fetcher, *, slug: str, config=None, source: str = "g2") -> SelfcheckResult:
    if source == "capterra":
        return _run_selfcheck_capterra(fetcher, slug=slug)
    if source == "trustradius":
        return _run_selfcheck_trustradius(fetcher, slug=slug)
    return _run_selfcheck_g2(fetcher, slug=slug)


def _run_selfcheck_g2(fetcher, *, slug: str) -> SelfcheckResult:
    from src.sources.marketplace.g2 import (
        extract_g2_reviews,
        g2_reviews_fragment_url,
    )
    from src.sources.techstack.datadome import is_datadome_challenge

    url = g2_reviews_fragment_url(slug, page=None)
    return run_source_selfcheck(
        fetcher,
        url=url,
        source="marketplace_g2",
        domain="g2.com",
        extract=lambda html: extract_g2_reviews(html, slug),
        markup_hints=[b"elv-stars", b"-review-"],
        detect_challenge=lambda status, body: is_datadome_challenge(
            status=status, body=body),
        fetch_kwargs=dict(wait_ms=4000, scroll=True,
                          warmup_url="https://www.g2.com/", warmup_ms=4000),
        drift_detail="review markup present but parser extracted 0 — selectors stale",
    )


def _run_selfcheck_capterra(fetcher, *, slug: str) -> SelfcheckResult:
    from src.sources.marketplace.capterra import extract_capterra_reviews
    from src.sources.techstack.datadome import is_datadome_challenge

    url = capterra_reviews_url(slug)

    def _challenge(status, body):
        return (is_datadome_challenge(status=status, body=body)
                or any(m in body for m in _CAPTERRA_CHALLENGE_MARKERS))

    # Capture the fetched body so the consent-banner note can be attached to
    # the result detail without breaking run_source_selfcheck's contract.
    captured: dict = {}

    class _RecordingFetcher:
        def __init__(self, inner):
            self._inner = inner

        def fetch(self, u, **kwargs):
            r = self._inner.fetch(u, **kwargs)
            try:
                captured["body"] = (r.doc.body or b"") if r.doc else b""
            except Exception:  # noqa: BLE001 — body capture must never break the selfcheck
                captured["body"] = b""
            return r

    result = run_source_selfcheck(
        _RecordingFetcher(fetcher),
        url=url,
        source="marketplace_capterra",
        domain="capterra.com",
        extract=lambda html: extract_capterra_reviews(html, slug),
        markup_hints=[b"review-cards-container"],
        detect_challenge=_challenge,
        drift_detail="review-cards-container present but parser extracted 0 — selectors stale",
    )

    # Consent-gate tolerance: a OneTrust banner alongside valid cards is not
    # drift — note its presence in the detail when detected.
    if result.state == "ok" and b"onetrust" in captured.get("body", b"").lower():
        result.detail = "consent-banner present"
    return result


def _run_selfcheck_trustradius(fetcher, *, slug: str) -> SelfcheckResult:
    from src.sources.marketplace.trustradius import extract_trustradius_reviews
    from src.sources.techstack.datadome import is_datadome_challenge

    url = trustradius_reviews_url(slug)

    def _challenge(status, body):
        return (is_datadome_challenge(status=status, body=body)
                or any(m in body for m in _TRUSTRADIUS_CHALLENGE_MARKERS))

    return run_source_selfcheck(
        fetcher,
        url=url,
        source="marketplace_trustradius",
        domain="trustradius.com",
        extract=lambda html: extract_trustradius_reviews(html, slug),
        markup_hints=[b"data-testid='stars-container'",
                      b'data-testid="stars-container"',
                      b"Review_review"],
        detect_challenge=_challenge,
        drift_detail="review-card markup present but parser extracted 0 — selectors stale",
    )
