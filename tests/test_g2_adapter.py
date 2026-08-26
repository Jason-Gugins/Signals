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

def test_g2_follow_tasks_returns_next_page():
    """follow_tasks returns a FetchTask for page 2 when current page has reviews."""
    adapter = MarketplaceG2Source()
    acct = Account(domain="acme.com", g2_slug="slack")
    doc = Document(doc_id="d", source="marketplace_g2", url="https://www.g2.com/products/slack/reviews",
                   body=FIXTURE.encode("utf-8"))
    meta = {"today": "2026-03-15", "product_slug": "slack", "page": 1, "max_review_pages": 5}
    follows = adapter.follow_tasks(doc, acct, meta)
    assert len(follows) == 1
    assert "page=2" in follows[0].url

def test_g2_follow_tasks_stops_at_max_pages():
    """follow_tasks returns [] when current page >= max_review_pages."""
    adapter = MarketplaceG2Source()
    acct = Account(domain="acme.com", g2_slug="slack")
    doc = Document(doc_id="d", source="marketplace_g2", url="https://www.g2.com/products/slack/reviews?page=5",
                   body=FIXTURE.encode("utf-8"))
    meta = {"today": "2026-03-15", "product_slug": "slack", "page": 5, "max_review_pages": 5}
    follows = adapter.follow_tasks(doc, acct, meta)
    assert follows == []

def test_g2_follow_tasks_stops_on_empty_page():
    """follow_tasks returns [] when no reviews were found on the current page."""
    adapter = MarketplaceG2Source()
    acct = Account(domain="acme.com", g2_slug="slack")
    doc = Document(doc_id="d", source="marketplace_g2", url="https://www.g2.com/products/slack/reviews?page=3",
                   body=b"<html><body></body></html>")
    meta = {"today": "2026-03-15", "product_slug": "slack", "page": 3, "max_review_pages": 5}
    follows = adapter.follow_tasks(doc, acct, meta)
    assert follows == []

def test_g2_plan_with_session_cookies(tmp_path):
    """plan() includes session cookies in task headers when cookie file is configured."""
    import json
    cookie_file = tmp_path / "g2_cookies.json"
    cookie_file.write_text(json.dumps([
        {"name": "session", "value": "abc123", "domain": ".g2.com"},
        {"name": "cf_clearance", "value": "tok", "domain": ".g2.com"},
    ]))
    adapter = MarketplaceG2Source()
    adapter._session_cookie_file = str(cookie_file)
    acct = Account(domain="acme.com", g2_slug="slack")
    tasks = adapter.plan(acct, None)
    assert len(tasks) == 1
    assert "Cookie" in tasks[0].headers
    assert "session=abc123" in tasks[0].headers["Cookie"]
    assert "cf_clearance=tok" in tasks[0].headers["Cookie"]

def test_g2_plan_without_session_cookies():
    """plan() works normally when no cookie file is set."""
    adapter = MarketplaceG2Source()
    adapter._session_cookie_file = None
    acct = Account(domain="acme.com", g2_slug="slack")
    tasks = adapter.plan(acct, None)
    assert len(tasks) == 1
    assert "Cookie" not in tasks[0].headers
