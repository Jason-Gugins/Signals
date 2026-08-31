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
