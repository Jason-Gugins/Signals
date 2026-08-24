from pathlib import Path

from src.core.models import Account, Document
from src.sources.news.collector import CompanyFeedSource, GoogleNewsSource, NewsRssSource


def test_gnews_and_blog():
    src = NewsRssSource()
    g = Path("tests/fixtures/news/google_news.xml").read_bytes()
    cands = src.parse(
        Document(doc_id="g", source="news_rss", body=g),
        Account(domain="acme.com", name="Acme"),
        {"kind": "gnews", "today": "2026-08-16"},
    )
    assert any(c.signal_type == "funding_round" for c in cands)
    feed = CompanyFeedSource()
    blog = Path("tests/fixtures/news/company_blog.xml").read_bytes()
    bc = feed.parse(
        Document(doc_id="b", source="company_feed", url="https://acme.com/feed", body=blog),
        Account(domain="acme.com", name="Acme"),
        {"today": "2026-08-16"},
    )
    assert any(c.signal_type == "product_launch" for c in bc)


def test_google_news_source_plans_search_and_topic():
    """GoogleNewsSource plans both a keyword search and a TECHNOLOGY topic feed."""
    src = GoogleNewsSource()
    account = Account(domain="acme.com", name="Acme Corp")
    tasks = src.plan(account, cursor=None)
    # Should produce at least 2 tasks: keyword search + topic feed
    assert len(tasks) >= 2
    urls = [t.url for t in tasks]
    # Keyword search includes the account name
    assert any("rss/search" in u and "Acme" in u for u in urls)
    # Topic feed is a section/topic URL
    assert any("section/topic" in u for u in urls)


def test_google_news_source_parses_topic_feed():
    """GoogleNewsSource.parse() on a topic feed fixture classifies items
    that mention the account name."""
    src = GoogleNewsSource()
    feed = Path("tests/fixtures/news/google_news_technology.xml").read_bytes()
    cands = src.parse(
        Document(doc_id="t", source="google_news", body=feed),
        Account(domain="acme.com", name="Acme Corp"),
        {"kind": "topic", "topic": "TECHNOLOGY", "today": "2026-08-23"},
    )
    # Acme Corp appears in 2 of 3 items; both should classify
    types = {c.signal_type for c in cands}
    assert "funding_round" in types  # "raises $50M Series B"
    assert "exec_hire" in types  # "appoints new CTO"
    # Rival Inc item should NOT match (no "Acme" in text)
    assert all("acme" in (c.title or "").lower() or "acme" in (c.url or "").lower() for c in cands)


def test_google_news_plans_serp_keyword_searches():
    """For each SERP keyword, plan() emits an extra search task containing it."""
    src = GoogleNewsSource()
    account = Account(domain="acme.com", name="Acme Corp")
    tasks = src.plan(account, cursor=None)
    urls = [t.url for t in tasks]
    # Default plain search + topics still present
    assert any("section/topic" in u for u in urls)
    # Each default SERP keyword yields a search URL containing the keyword
    for kw in ["fundraising", "acquisition", "product launch", "CEO"]:
        assert any(kw.replace(" ", "+") in u for u in urls), kw
