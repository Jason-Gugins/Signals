"""Task 7: review-id stability across date-format re-renders.

Parity: the same review rendered with two different posted-date formats
("August 1, 2026" vs "2026-08-01") must produce the SAME review_id, so a
re-render of the same review does not churn its key into a duplicate 'new'
row. Regression: a genuinely different review still gets a different id.
"""

from src.sources.marketplace.capterra import extract_capterra_reviews


def _card_html(date_text: str, reviewer: str = "Jane Doe") -> str:
    return f"""
    <div data-test-id="review-cards-container">
      <div>
        <div data-testid="Overall Rating-rating">
          <i aria-label="star-full"></i><i aria-label="star-full"></i>
          <i aria-label="star-full"></i><i aria-label="star-full"></i>
        </div>
        <div class="typo-0 text-neutral-90">{date_text}</div>
        <div><span class="typo-20 font-semibold">{reviewer}</span><br>Senior PM</div>
        <h3>"Great tool"</h3>
        <span>Pros</span><p>Fast</p>
        <span>Cons</span><p>Pricey</p>
      </div>
    </div>
    """


def test_review_id_parity_across_date_formats():
    """Same review, two date renders -> same review_id."""
    a = extract_capterra_reviews(_card_html("August 1, 2026"), "jira")
    b = extract_capterra_reviews(_card_html("2026-08-01"), "jira")
    assert len(a) == 1 and len(b) == 1
    assert a[0].posted_at == "2026-08-01"
    assert b[0].posted_at == "2026-08-01"
    assert a[0].review_id == b[0].review_id


def test_review_id_differs_for_different_reviews():
    a = extract_capterra_reviews(_card_html("August 1, 2026"), "jira")
    b = extract_capterra_reviews(
        _card_html("August 1, 2026", reviewer="Bob Smith"), "jira")
    assert len(a) == 1 and len(b) == 1
    assert a[0].review_id != b[0].review_id
