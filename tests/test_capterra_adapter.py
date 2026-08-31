from pathlib import Path
from src.core.models import Account, Document
from src.sources.marketplace.collector import MarketplaceCapterraSource

FIXTURE = (Path("tests/fixtures/marketplace/capterra_jira_reviews.html")).read_text(encoding="utf-8")


def test_capterra_plan_requires_slug():
    adapter = MarketplaceCapterraSource()
    acct = Account(domain="acme.com")  # no slugs
    assert adapter.plan(acct, None) == []


def test_capterra_plan_single_segment_returns_reviews_url():
    adapter = MarketplaceCapterraSource()
    acct = Account(domain="acme.com", g2_slug="19319/JIRA")
    tasks = adapter.plan(acct, None)
    assert len(tasks) == 1
    assert tasks[0].url == "https://www.capterra.com/p/19319/JIRA/reviews/"
    assert tasks[0].source == "marketplace_capterra"
    assert tasks[0].domain == "acme.com"
    assert tasks[0].meta["product_slug"] == "19319/JIRA"
    assert tasks[0].meta["page"] == 1


def test_capterra_plan_fans_out_multi_segment():
    adapter = MarketplaceCapterraSource()
    acct = Account(domain="db.com", g2_slug="19319/JIRA,211559/Trello")
    tasks = adapter.plan(acct, None)
    assert len(tasks) == 2
    slugs = [t.meta["product_slug"] for t in tasks]
    assert slugs == ["19319/JIRA", "211559/Trello"]
    assert all(t.url == f"https://www.capterra.com/p/{s}/reviews/" for t, s in zip(tasks, slugs))
    assert all(t.source == "marketplace_capterra" for t in tasks)


def test_capterra_plan_multi_segment_strips_whitespace():
    adapter = MarketplaceCapterraSource()
    acct = Account(domain="db.com", g2_slug=" 19319/JIRA , 211559/Trello ,")
    tasks = adapter.plan(acct, None)
    assert [t.meta["product_slug"] for t in tasks] == ["19319/JIRA", "211559/Trello"]


def test_capterra_plan_sets_meta_defaults():
    adapter = MarketplaceCapterraSource()
    acct = Account(domain="acme.com", g2_slug="19319/JIRA")
    tasks = adapter.plan(acct, None)
    assert tasks[0].meta["review_lookback_days"] == 90
    assert tasks[0].meta["max_review_pages"] == 3


def test_capterra_plan_without_session_cookies():
    adapter = MarketplaceCapterraSource()
    adapter._session_cookie_file = None
    acct = Account(domain="acme.com", g2_slug="19319/JIRA")
    tasks = adapter.plan(acct, None)
    assert len(tasks) == 1
    assert "Cookie" not in tasks[0].headers


def test_capterra_plan_with_session_cookies(tmp_path):
    import json
    cookie_file = tmp_path / "capterra_cookies.json"
    cookie_file.write_text(json.dumps([
        {"name": "session", "value": "abc123", "domain": ".capterra.com"},
        {"name": "cf_clearance", "value": "tok", "domain": ".capterra.com"},
    ]))
    adapter = MarketplaceCapterraSource()
    adapter._session_cookie_file = str(cookie_file)
    acct = Account(domain="acme.com", g2_slug="19319/JIRA")
    tasks = adapter.plan(acct, None)
    assert len(tasks) == 1
    assert "Cookie" in tasks[0].headers
    assert "session=abc123" in tasks[0].headers["Cookie"]
    assert "cf_clearance=tok" in tasks[0].headers["Cookie"]


def test_capterra_parse_returns_signal_candidates():
    adapter = MarketplaceCapterraSource()
    acct = Account(domain="acme.com", name="Acme", g2_slug="19319/JIRA")
    doc = Document(doc_id="d", source="marketplace_capterra",
                   url="https://www.capterra.com/p/19319/JIRA/reviews/",
                   body=FIXTURE.encode("utf-8"))
    # Far-future-tolerant lookback: all 25 fixture reviews within range
    meta = {"today": "2026-08-28", "review_lookback_days": 400, "product_slug": "19319/JIRA"}
    cands = adapter.parse(doc, acct, meta)
    assert len(cands) == 25
    assert all(c.signal_type == "intent_2nd_marketplace" for c in cands)
    assert all(c.confidence > 0 for c in cands)
    assert all(c.evidence_data["product_slug"] == "19319/JIRA" for c in cands)
    assert all(c.natural_key.startswith("caprev:19319/JIRA:") for c in cands)


def test_capterra_parse_skips_old_reviews():
    adapter = MarketplaceCapterraSource()
    acct = Account(domain="acme.com", g2_slug="19319/JIRA")
    doc = Document(doc_id="d", source="marketplace_capterra",
                   url="https://www.capterra.com/p/19319/JIRA/reviews/",
                   body=FIXTURE.encode("utf-8"))
    # Default 90-day lookback from 2026-08-28 excludes Sep 2025/Nov 2025/Jan 2026 reviews
    meta = {"today": "2026-08-28", "product_slug": "19319/JIRA"}
    cands = adapter.parse(doc, acct, meta)
    posted = [c.observed_at for c in cands]
    assert all(p >= "2026-05-30" for p in posted)
    assert cands  # some recent reviews exist in the fixture


def test_capterra_parse_empty_body_returns_empty():
    adapter = MarketplaceCapterraSource()
    acct = Account(domain="acme.com", g2_slug="19319/JIRA")
    doc = Document(doc_id="d", source="marketplace_capterra",
                   url="https://www.capterra.com/p/19319/JIRA/reviews/", body=b"<html></html>")
    meta = {"today": "2026-08-28"}
    assert adapter.parse(doc, acct, meta) == []


def test_capterra_harvest_reviews_returns_capterra_reviews():
    from src.sources.marketplace.capterra import CapterraReview
    adapter = MarketplaceCapterraSource()
    acct = Account(domain="acme.com", g2_slug="19319/JIRA")
    doc = Document(doc_id="d", source="marketplace_capterra",
                   url="https://www.capterra.com/p/19319/JIRA/reviews/",
                   body=FIXTURE.encode("utf-8"))
    reviews = adapter.harvest_reviews(doc, acct, {"product_slug": "19319/JIRA"})
    assert len(reviews) == 25
    assert all(isinstance(r, CapterraReview) for r in reviews)
    assert all(r.product_slug == "19319/JIRA" for r in reviews)


def test_capterra_follow_tasks_returns_next_page():
    adapter = MarketplaceCapterraSource()
    acct = Account(domain="acme.com", g2_slug="19319/JIRA")
    doc = Document(doc_id="d", source="marketplace_capterra",
                   url="https://www.capterra.com/p/19319/JIRA/reviews/",
                   body=FIXTURE.encode("utf-8"))
    meta = {"today": "2026-08-28", "product_slug": "19319/JIRA", "page": 1, "max_review_pages": 3}
    follows = adapter.follow_tasks(doc, acct, meta)
    assert len(follows) == 1
    assert follows[0].url == "https://www.capterra.com/p/19319/JIRA/reviews/?page=2"
    assert follows[0].source == "marketplace_capterra"
    assert follows[0].meta["product_slug"] == "19319/JIRA"
    assert follows[0].meta["page"] == 2


def test_capterra_follow_tasks_stops_at_max_pages():
    adapter = MarketplaceCapterraSource()
    acct = Account(domain="acme.com", g2_slug="19319/JIRA")
    doc = Document(doc_id="d", source="marketplace_capterra",
                   url="https://www.capterra.com/p/19319/JIRA/reviews/?page=3",
                   body=FIXTURE.encode("utf-8"))
    meta = {"today": "2026-08-28", "product_slug": "19319/JIRA", "page": 3, "max_review_pages": 3}
    assert adapter.follow_tasks(doc, acct, meta) == []


def test_capterra_follow_tasks_stops_on_empty_page():
    adapter = MarketplaceCapterraSource()
    acct = Account(domain="acme.com", g2_slug="19319/JIRA")
    doc = Document(doc_id="d", source="marketplace_capterra",
                   url="https://www.capterra.com/p/19319/JIRA/reviews/?page=2",
                   body=b"<html><body></body></html>")
    meta = {"today": "2026-08-28", "product_slug": "19319/JIRA", "page": 2, "max_review_pages": 3}
    assert adapter.follow_tasks(doc, acct, meta) == []
