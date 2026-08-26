from datetime import date
from pathlib import Path
from src.core.models import Account, Document
from src.sources.marketplace.collector import MarketplaceG2Source

FIXTURE = (Path("tests/fixtures/marketplace/g2_reviews.html")).read_text(encoding="utf-8")

def test_g2_plan_requires_g2_slug():
    adapter = MarketplaceG2Source()
    acct = Account(domain="acme.com")  # no g2_slug
    tasks = adapter.plan(acct, None)
    assert tasks == []

def test_g2_plan_returns_review_url():
    adapter = MarketplaceG2Source()
    acct = Account(domain="acme.com", g2_slug="slack")
    tasks = adapter.plan(acct, None)
    assert len(tasks) == 1
    assert tasks[0].url == "https://www.g2.com/products/slack/reviews"
    assert tasks[0].source == "marketplace_g2"
    assert tasks[0].domain == "acme.com"

def test_g2_parse_returns_signal_candidates():
    adapter = MarketplaceG2Source()
    acct = Account(domain="acme.com", name="Acme", g2_slug="slack")
    doc = Document(doc_id="d", source="marketplace_g2", url="https://www.g2.com/products/slack/reviews",
                   body=FIXTURE.encode("utf-8"))
    # Use today=2026-02-01 so the Jan 15 review is within 90 days
    meta = {"today": "2026-02-01"}
    cands = adapter.parse(doc, acct, meta)
    assert len(cands) >= 1
    assert all(c.signal_type == "intent_2nd_marketplace" for c in cands)
    assert all(c.confidence > 0 for c in cands)

def test_g2_parse_skips_old_reviews():
    adapter = MarketplaceG2Source()
    acct = Account(domain="acme.com", g2_slug="slack")
    doc = Document(doc_id="d", source="marketplace_g2", url="https://www.g2.com/products/slack/reviews",
                   body=FIXTURE.encode("utf-8"))
    # All fixture reviews (Jan 2026, Dec 2025, Nov 2025) are > 90 days from Aug 2026
    meta = {"today": "2026-08-25"}
    cands = adapter.parse(doc, acct, meta)
    assert cands == []

def test_g2_parse_respects_custom_lookback_days():
    """parse() should use review_lookback_days from task_meta."""
    adapter = MarketplaceG2Source()
    acct = Account(domain="acme.com", g2_slug="slack")
    doc = Document(doc_id="d", source="marketplace_g2", url="https://www.g2.com/products/slack/reviews",
                   body=FIXTURE.encode("utf-8"))
    # With lookback=365, all 3 fixture reviews (Jan, Dec, Nov) are within range from 2026-03-15
    meta = {"today": "2026-03-15", "review_lookback_days": 365}
    cands = adapter.parse(doc, acct, meta)
    assert len(cands) == 3

    # With lookback=30, all reviews > 30 days from 2026-03-15
    meta = {"today": "2026-03-15", "review_lookback_days": 30}
    cands = adapter.parse(doc, acct, meta)
    assert cands == []
