"""Task 7: Capterra consent-gate tolerance.

A page containing the OneTrust consent banner PLUS valid review cards must
still parse identically — the banner must not break extraction — and the
capterra selfcheck wrapper must report state 'ok' with a
'consent-banner present' note in the result detail.
"""

from types import SimpleNamespace

from src.sources.marketplace.capterra import extract_capterra_reviews
from src.sources.marketplace.selfcheck import run_selfcheck

_BANNER = """
<div id="onetrust-consent-sdk">
  <div id="onetrust-banner-sdk" class="otFlat">
    <div id="onetrust-button-group">
      <button id="onetrust-accept-btn-handler">Accept All Cookies</button>
    </div>
  </div>
</div>
"""


def _cards_html(n: int = 2) -> str:
    cards = "".join(f"""
    <div>
      <div data-testid="Overall Rating-rating">
        <i aria-label="star-full"></i><i aria-label="star-full"></i>
        <i aria-label="star-full"></i><i aria-label="star-full"></i>
      </div>
      <div class="typo-0 text-neutral-90">August 1, 2026</div>
      <div><span class="typo-20 font-semibold">Reviewer {i}</span><br>PM</div>
      <h3>"Fine"</h3>
      <span>Pros</span><p>Fast</p>
      <span>Cons</span><p>Pricey</p>
    </div>""" for i in range(n))
    return f'<div data-test-id="review-cards-container">{cards}</div>'


def test_banner_inside_card_container_is_ignored():
    """Banner markup injected as a direct child of the card container must
    not change the extracted reviews."""
    base = extract_capterra_reviews(_cards_html(2), "jira")
    injected = extract_capterra_reviews(
        _cards_html(2).replace(
            "<div>",
            f"{_BANNER}<div>", 1),
        "jira")
    assert len(base) == 2
    assert len(injected) == 2
    assert [r.review_id for r in injected] == [r.review_id for r in base]


def test_banner_before_container_does_not_break_extraction():
    html = f"<html><body>{_BANNER}{_cards_html(3)}</body></html>"
    reviews = extract_capterra_reviews(html, "jira")
    assert len(reviews) == 3


class _FakeFetcher:
    def __init__(self, body: bytes):
        self._body = body

    def fetch(self, url, **kwargs):
        return SimpleNamespace(ok=True, status=200,
                               doc=SimpleNamespace(body=self._body))


def test_selfcheck_ok_with_consent_banner_note():
    html = f"<html><body>{_BANNER}{_cards_html(2)}</body></html>"
    result = run_selfcheck(_FakeFetcher(html.encode()), slug="19319/JIRA",
                           source="capterra")
    assert result.state == "ok"
    assert result.review_count == 2
    assert "consent-banner present" in result.detail


def test_selfcheck_ok_without_banner_has_no_consent_note():
    html = f"<html><body>{_cards_html(2)}</body></html>"
    result = run_selfcheck(_FakeFetcher(html.encode()), slug="19319/JIRA",
                           source="capterra")
    assert result.state == "ok"
    assert result.review_count == 2
    assert "consent-banner present" not in result.detail
