from src.sources.marketplace.collector import MarketplaceG2Source
from src.core.models import Account, Document

FIXTURE_BODY = b"<html><body><div class='paper'>review</div></body></html>"

def test_g2_deep_reviews_meta_not_set_by_default():
    """plan() does not set click_show_more by default."""
    adapter = MarketplaceG2Source()
    acct = Account(domain="acme.com", g2_slug="slack")
    tasks = adapter.plan(acct, None)
    assert tasks[0].meta.get("click_show_more") is None

def test_g2_parse_works_with_click_show_more_meta():
    """parse() works normally when click_show_more is in meta."""
    adapter = MarketplaceG2Source()
    acct = Account(domain="acme.com", g2_slug="slack")
    doc = Document(doc_id="d", source="marketplace_g2",
                   url="https://www.g2.com/products/slack/reviews",
                   body=FIXTURE_BODY)
    meta = {"today": "2026-03-15", "click_show_more": True}
    cands = adapter.parse(doc, acct, meta)
    assert cands == []

def test_browser_fetch_accepts_click_show_more():
    """BrowserFetcher.fetch() accepts click_show_more kwarg without error."""
    import inspect
    from src.core.browser import BrowserFetcher
    sig = inspect.signature(BrowserFetcher.fetch)
    assert "click_show_more" in sig.parameters

def test_cf_bypass_attempt_accepts_click_show_more():
    """CloudflareBypass.attempt() accepts click_show_more kwarg."""
    import inspect
    from src.sources.techstack.cf_bypass import CloudflareBypass
    sig = inspect.signature(CloudflareBypass.attempt)
    assert "click_show_more" in sig.parameters
