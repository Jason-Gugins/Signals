from pathlib import Path

from src.core.models import Account, Document
from src.sources.news.collector import CompanyFeedSource, NewsRssSource


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
