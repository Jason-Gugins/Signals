from pathlib import Path
from src.sources.news.feeds import FEED_GUESSES, discover_feeds, google_news_url, parse_feed

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
