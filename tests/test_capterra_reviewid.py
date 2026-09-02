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


# --- unknown-date hash fallback (P3 Batch 2, MINOR 2) ----------------------

def test_review_id_unknown_date_falls_back_to_raw_date():
    """Unparseable dates hash the raw rendered string, not the literal 'None'."""
    a = extract_capterra_reviews(_card_html("Sometime recently"), "jira")
    b = extract_capterra_reviews(_card_html("Sometime earlier"), "jira")
    assert len(a) == 1 and len(b) == 1
    assert a[0].posted_at is None
    assert b[0].posted_at is None
    # distinct raw date strings -> distinct ids (no 'None' collision)
    assert a[0].review_id != b[0].review_id
    # deterministic across re-renders
    a2 = extract_capterra_reviews(_card_html("Sometime recently"), "jira")
    assert a2[0].review_id == a[0].review_id


# --- review-id adoption on scheme change (P3 Batch 2, MAJOR 1) -------------

def test_upsert_adopts_legacy_row_matching_natural_key(tmp_path):
    """A legacy row (old-style id) with same (slug, reviewer, posted_at) is
    ADOPTED — review_id updated in place — rather than duplicated."""
    import sqlite3

    from src.core.db import Database
    from src.sources.marketplace.capterra import CapterraReview
    from src.sources.marketplace.collector import upsert_capterra_reviews

    db = Database(tmp_path / "s.db")
    # Legacy-shaped row: old-scheme review_id, same natural key.
    db.upsert(
        "g2_reviews",
        {
            "review_id": "legacy-old-id-01",
            "product_slug": "19319/JIRA",
            "reviewer_name": "Kim F.",
            "posted_at": "2026-06-29",
            "source": "capterra",
            "first_seen_at": "2026-07-01T00:00:00+00:00",
            "last_seen_at": "2026-07-01T00:00:00+00:00",
        },
        pk=("review_id",),
    )
    legacy_id = db.one(
        "SELECT rowid FROM g2_reviews WHERE review_id=?", ("legacy-old-id-01",)
    )["rowid"]

    candidate = CapterraReview(
        review_id="cap-new-scheme-1",
        product_slug="19319/JIRA",
        reviewer_name="Kim F.",
        rating=4.5,
        posted_at="2026-06-29",
        verified_reviewer=False,
    )
    new, updated = upsert_capterra_reviews(
        db, [candidate], now="2026-08-30T00:00:00+00:00"
    )
    # adopted, not inserted
    assert (new, updated) == (0, 1)
    count = db.one("SELECT COUNT(*) AS n FROM g2_reviews")["n"]
    assert count == 1
    row = db.one("SELECT rowid, review_id, first_seen_at, source FROM g2_reviews")
    assert row["rowid"] == legacy_id  # same physical row updated in place
    assert row["review_id"] == "cap-new-scheme-1"  # id adopted
    assert row["first_seen_at"] == "2026-07-01T00:00:00+00:00"  # provenance kept
    assert row["source"] == "capterra"
