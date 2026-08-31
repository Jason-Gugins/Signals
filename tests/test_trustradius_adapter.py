from pathlib import Path
from src.core.models import Account, Document
from src.sources.marketplace.collector import MarketplaceTrustRadiusSource

FIXTURE = (Path("tests/fixtures/marketplace/trustradius_slack_reviews.html")).read_text(encoding="utf-8")
URL = "https://www.trustradius.com/products/slack/reviews"


def test_trustradius_plan_requires_slug():
    adapter = MarketplaceTrustRadiusSource()
    acct = Account(domain="acme.com")  # no slugs
    assert adapter.plan(acct, None) == []


def test_trustradius_plan_single_slug_returns_reviews_url():
    adapter = MarketplaceTrustRadiusSource()
    acct = Account(domain="acme.com", g2_slug="slack")
    tasks = adapter.plan(acct, None)
    assert len(tasks) == 1
    assert tasks[0].url == URL
    assert tasks[0].source == "marketplace_trustradius"
    assert tasks[0].domain == "acme.com"
    assert tasks[0].meta["product_slug"] == "slack"
    assert tasks[0].meta["page"] == 1


def test_trustradius_plan_fans_out_multi_slug():
    adapter = MarketplaceTrustRadiusSource()
    acct = Account(domain="db.com", g2_slug="slack,trello")
    tasks = adapter.plan(acct, None)
    assert len(tasks) == 2
    slugs = [t.meta["product_slug"] for t in tasks]
    assert slugs == ["slack", "trello"]
    assert all(t.url == f"https://www.trustradius.com/products/{s}/reviews"
               for t, s in zip(tasks, slugs))
    assert all(t.source == "marketplace_trustradius" for t in tasks)


def test_trustradius_plan_multi_slug_strips_whitespace():
    adapter = MarketplaceTrustRadiusSource()
    acct = Account(domain="db.com", g2_slug=" slack , trello ,")
    tasks = adapter.plan(acct, None)
    assert [t.meta["product_slug"] for t in tasks] == ["slack", "trello"]


def test_trustradius_plan_sets_meta_defaults():
    adapter = MarketplaceTrustRadiusSource()
    acct = Account(domain="acme.com", g2_slug="slack")
    tasks = adapter.plan(acct, None)
    assert tasks[0].meta["review_lookback_days"] == 90
    assert tasks[0].meta["max_review_pages"] == 2


def test_trustradius_plan_with_session_cookies(tmp_path):
    import json
    cookie_file = tmp_path / "tr_cookies.json"
    cookie_file.write_text(json.dumps([
        {"name": "session", "value": "abc123", "domain": ".trustradius.com"},
    ]))
    adapter = MarketplaceTrustRadiusSource()
    adapter._session_cookie_file = str(cookie_file)
    acct = Account(domain="acme.com", g2_slug="slack")
    tasks = adapter.plan(acct, None)
    assert len(tasks) == 1
    assert "Cookie" in tasks[0].headers
    assert "session=abc123" in tasks[0].headers["Cookie"]


def test_trustradius_parse_returns_signal_candidates():
    adapter = MarketplaceTrustRadiusSource()
    acct = Account(domain="acme.com", name="Acme", g2_slug="slack")
    doc = Document(doc_id="d", source="marketplace_trustradius", url=URL,
                   body=FIXTURE.encode("utf-8"))
    meta = {"today": "2026-08-28", "review_lookback_days": 400, "product_slug": "slack"}
    cands = adapter.parse(doc, acct, meta)
    assert len(cands) == 3
    assert all(c.signal_type == "intent_2nd_marketplace" for c in cands)
    assert all(c.confidence > 0 for c in cands)
    assert all(c.evidence_data["product_slug"] == "slack" for c in cands)
    assert all(c.natural_key.startswith("trrev:slack:") for c in cands)


def test_trustradius_parse_skips_old_reviews():
    adapter = MarketplaceTrustRadiusSource()
    acct = Account(domain="acme.com", g2_slug="slack")
    doc = Document(doc_id="d", source="marketplace_trustradius", url=URL,
                   body=FIXTURE.encode("utf-8"))
    meta = {"today": "2026-08-28", "product_slug": "slack"}
    cands = adapter.parse(doc, acct, meta)
    posted = [c.observed_at for c in cands]
    assert all(p >= "2026-05-30" for p in posted)
    assert cands  # fixture cards are Aug 2026 — recent


def test_trustradius_parse_empty_body_returns_empty():
    adapter = MarketplaceTrustRadiusSource()
    acct = Account(domain="acme.com", g2_slug="slack")
    doc = Document(doc_id="d", source="marketplace_trustradius", url=URL,
                   body=b"<html></html>")
    meta = {"today": "2026-08-28"}
    assert adapter.parse(doc, acct, meta) == []


def test_trustradius_harvest_reviews_returns_trustradius_reviews():
    from src.sources.marketplace.trustradius import TrustRadiusReview
    adapter = MarketplaceTrustRadiusSource()
    acct = Account(domain="acme.com", g2_slug="slack")
    doc = Document(doc_id="d", source="marketplace_trustradius", url=URL,
                   body=FIXTURE.encode("utf-8"))
    reviews = adapter.harvest_reviews(doc, acct, {"product_slug": "slack"})
    assert len(reviews) == 3
    assert all(isinstance(r, TrustRadiusReview) for r in reviews)
    assert all(r.product_slug == "slack" for r in reviews)


def test_trustradius_follow_tasks_returns_next_page():
    adapter = MarketplaceTrustRadiusSource()
    acct = Account(domain="acme.com", g2_slug="slack")
    doc = Document(doc_id="d", source="marketplace_trustradius", url=URL,
                   body=FIXTURE.encode("utf-8"))
    meta = {"today": "2026-08-28", "product_slug": "slack", "page": 1, "max_review_pages": 2}
    follows = adapter.follow_tasks(doc, acct, meta)
    assert len(follows) == 1
    assert follows[0].url == f"{URL}?page=2"
    assert follows[0].source == "marketplace_trustradius"
    assert follows[0].meta["product_slug"] == "slack"
    assert follows[0].meta["page"] == 2


def test_trustradius_follow_tasks_stops_at_max_pages():
    adapter = MarketplaceTrustRadiusSource()
    acct = Account(domain="acme.com", g2_slug="slack")
    doc = Document(doc_id="d", source="marketplace_trustradius", url=f"{URL}?page=2",
                   body=FIXTURE.encode("utf-8"))
    meta = {"today": "2026-08-28", "product_slug": "slack", "page": 2, "max_review_pages": 2}
    assert adapter.follow_tasks(doc, acct, meta) == []


def test_trustradius_follow_tasks_stops_on_empty_page():
    adapter = MarketplaceTrustRadiusSource()
    acct = Account(domain="acme.com", g2_slug="slack")
    doc = Document(doc_id="d", source="marketplace_trustradius", url=f"{URL}?page=2",
                   body=b"<html><body></body></html>")
    meta = {"today": "2026-08-28", "product_slug": "slack", "page": 2, "max_review_pages": 2}
    assert adapter.follow_tasks(doc, acct, meta) == []
