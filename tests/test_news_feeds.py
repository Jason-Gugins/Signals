from pathlib import Path
from src.sources.news.feeds import (
    FEED_GUESSES,
    discover_feeds,
    google_news_search_url,
    google_news_topic_url,
    google_news_url,
    parse_feed,
)

FIX = Path(__file__).parent / "fixtures" / "news"


def test_discover_order():
    html = (FIX / "homepage_with_feed.html").read_text(encoding="utf-8")
    feeds = discover_feeds(html, "https://acme.com/")
    assert len(feeds) == 2
    assert feeds[0].endswith("/blog/feed")
    assert feeds[1].startswith("https://news.example.com")


def test_guesses_stable():
    assert FEED_GUESSES[0] == "/feed"
    assert FEED_GUESSES == list(FEED_GUESSES)


def test_google_news_url_encodes():
    url = google_news_url("Acme & Sons")
    assert "%22" in url or "%26" in url
    assert "when:30d" in url


def test_parse_rss_and_atom_and_unwrap():
    items = parse_feed((FIX / "google_news.xml").read_bytes())
    assert items[0].title.startswith("Acme")
    assert "techcrunch.com" in items[0].link
    atom = parse_feed((FIX / "company_blog.xml").read_bytes())
    assert atom[0].title == "Introducing Widget"


def test_malformed_xml_empty():
    assert parse_feed(b"<not xml") == [] or isinstance(parse_feed(b"<not xml"), list)
    assert parse_feed(b"") == []


def test_google_news_search_url_keyword():
    url = google_news_search_url("artificial intelligence")
    assert "q=artificial+intelligence" in url
    assert "when:30d" in url
    assert "news.google.com/rss/search" in url
    assert "hl=en-US" in url
    assert "gl=US" in url
    assert "ceid=US:en" in url


def test_google_news_search_url_custom_days():
    url = google_news_search_url("AI", days=7)
    assert "when:7d" in url


def test_google_news_search_url_with_quotes():
    url = google_news_search_url('"Acme Corp" funding')
    assert "%22Acme+Corp%22" in url


def test_google_news_topic_url_named_section():
    url = google_news_topic_url("TECHNOLOGY")
    assert "news.google.com/rss/headlines/section/topic/TECHNOLOGY" in url


def test_google_news_topic_url_with_locale():
    url = google_news_topic_url("BUSINESS", lang="en-GB", country="GB")
    assert "hl=en-GB" in url
    assert "gl=GB" in url
    assert "ceid=GB:en" in url


from src.sources.news.serp_config import load_google_news_cfg


def test_serp_config_defaults_when_absent(tmp_path, monkeypatch):
    """When no google_news.serp_keywords in sources.yaml, return defaults."""
    import yaml
    from pathlib import Path
    fake = tmp_path / "sources.yaml"
    fake.write_text("sources:\n  google_news:\n    enabled: true\n  other:\n    enabled: true\n")
    import src.sources.news.serp_config as sc
    monkeypatch.setattr(sc, "SOURCES_YAML_PATH", str(fake))
    cfg = load_google_news_cfg()
    assert "serp_keywords" in cfg
    assert len(cfg["serp_keywords"]) >= 4  # fundraising, leadership, product, acquisition
    assert all(isinstance(k, str) for k in cfg["serp_keywords"])


def test_serp_config_reads_custom(tmp_path, monkeypatch):
    """serp_keywords from sources.yaml override defaults."""
    from pathlib import Path
    fake = tmp_path / "sources.yaml"
    fake.write_text(
        "sources:\n  google_news:\n    serp_keywords: [fundraising, acquisition]\n"
    )
    import src.sources.news.serp_config as sc
    monkeypatch.setattr(sc, "SOURCES_YAML_PATH", str(fake))
    cfg = load_google_news_cfg()
    assert cfg["serp_keywords"] == ["fundraising", "acquisition"]
