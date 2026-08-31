"""g2-selfcheck: live selector-drift alarm (fetcher mocked — no network in tests)."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.core.http import FetchResult
from src.core.models import Document
from src.sources.marketplace.selfcheck import run_selfcheck, SelfcheckResult

GOOD = b"<html><article id='sierra-review-1' ue='track-in-viewport'>x</article></html>"
CHALLENGE = b"<html><script>var dd={'rt':'i','host':'geo.captcha-delivery.com'}</script></html>"
EMPTY = b"<html class='no-reviews-yet'></html>"


def _fetcher_returning(body):
    f = MagicMock()
    f.fetch.return_value = FetchResult(ok=True, status=200,
        doc=Document(doc_id="d", source="marketplace_g2",
                     url="https://www.g2.com/products/sierra/reviews_and_filters",
                     body=body), cached=False, error=None, elapsed_ms=10)
    return f


def test_selfcheck_ok_when_reviews_parse():
    r = run_selfcheck(_fetcher_returning(GOOD), slug="sierra")
    assert r.state == "ok" and r.review_count >= 1


def test_selfcheck_challenge_state():
    r = run_selfcheck(_fetcher_returning(CHALLENGE), slug="sierra")
    assert r.state == "challenge" and r.review_count == 0


def test_selfcheck_empty_state():
    r = run_selfcheck(_fetcher_returning(EMPTY), slug="sierra")
    assert r.state == "empty"


def test_selfcheck_drift_state_when_200_but_zero_parsed():
    """A page that LOOKS like it has reviews (review ids present) but parses to
    0 reviews = selector drift — the alarm case."""
    # Article without the id/ue pattern the parser selects on, but the page still
    # carries review markup (elv-stars, "-review-") — markup present, 0 parsed.
    drifted = (b"<html><article class='review-card'>x</article>"
               b"<div class='elv-stars elv-stars-9'></div>"
               b"<span data-g2-review-id='sierra-review-1'></span></html>")
    f = MagicMock()
    f.fetch.return_value = FetchResult(ok=True, status=200,
        doc=Document(doc_id="d", source="marketplace_g2",
                     url="https://www.g2.com/products/sierra/reviews_and_filters",
                     body=drifted), cached=False, error=None, elapsed_ms=10)
    r = run_selfcheck(f, slug="sierra")
    assert r.state == "drift"


# ---------------------------------------------------------------- Capterra ---
# Capterra is server-rendered, so the self-check uses the plain HTTP fetcher
# (no stealth browser). The mocked fetcher here returns a Document whose body
# carries the Capterra review-card markup (data-test-id attributes).

CAP_GOOD = (b"<html><div data-test-id='review-cards-container'>"
            b"<div><div data-testid='Overall Rating-rating'>"
            b"<i role='img' aria-label='star-full' data-rating='1'></i>"
            b"<i role='img' aria-label='star-full' data-rating='2'></i>"
            b"<i role='img' aria-label='star-full' data-rating='3'></i>"
            b"<i role='img' aria-label='star-full' data-rating='4'></i>"
            b"<i role='img' aria-label='star-full' data-rating='5'></i>"
            b"<span>5.0</span></div>"
            b"<span class='typo-20 font-semibold'>Kim F.</span>"
            b"<h3>Great Jira</h3><div class='typo-0 text-neutral-90'>July 11, 2026</div>"
            b"<span>Pros</span><p>Great tool for teams.</p>"
            b"</div></div></html>")
CAP_CHALLENGE = b"<html><head><title>Just a moment...</title></head><body></body></html>"
CAP_EMPTY = b"<html class='no-reviews-yet'></html>"
CAP_DRIFT = b"<html><div data-test-id='review-cards-container'><div class='stale-card'></div></div></html>"


def _capterra_fetcher_returning(body):
    f = MagicMock()
    f.fetch.return_value = FetchResult(ok=True, status=200,
        doc=Document(doc_id="d", source="marketplace_capterra",
                     url="https://www.capterra.com/p/19319/JIRA/reviews/",
                     body=body), cached=False, error=None, elapsed_ms=10)
    return f


def test_capterra_selfcheck_ok():
    r = run_selfcheck(_capterra_fetcher_returning(CAP_GOOD), slug="19319/JIRA",
                      source="capterra")
    assert r.state == "ok" and r.review_count >= 1
    assert r.url == "https://www.capterra.com/p/19319/JIRA/reviews/"


def test_capterra_selfcheck_empty():
    r = run_selfcheck(_capterra_fetcher_returning(CAP_EMPTY), slug="19319/JIRA",
                      source="capterra")
    assert r.state == "empty"


def test_capterra_selfcheck_drift():
    """Container present but 0 parsed = selector drift."""
    r = run_selfcheck(_capterra_fetcher_returning(CAP_DRIFT), slug="19319/JIRA",
                      source="capterra")
    assert r.state == "drift"


def test_capterra_selfcheck_challenge_cf_body():
    """Cloudflare challenge bodies ('Just a moment') also map to challenge."""
    r = run_selfcheck(_capterra_fetcher_returning(CAP_CHALLENGE), slug="19319/JIRA",
                      source="capterra")
    assert r.state == "challenge"


def test_capterra_selfcheck_error():
    f = MagicMock()
    f.fetch.side_effect = RuntimeError("connection refused")
    r = run_selfcheck(f, slug="19319/JIRA", source="capterra")
    assert r.state == "error"
