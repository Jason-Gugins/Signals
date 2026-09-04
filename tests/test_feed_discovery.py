from pathlib import Path

from src.core.db import Database
from src.core.models import Account, Document
from src.identity.feed_discovery import FeedDiscovery
from src.identity.registry import AccountRegistry

HTML = Path("tests/fixtures/news/homepage_with_feed.html").read_text(encoding="utf-8")


class Fake:
    def __init__(self):
        self.urls = []

    def get(self, task, **kw):
        from src.core.http import FetchResult

        self.urls.append(task.url)
        body = HTML.encode() if (task.url or "").rstrip("/") == "https://acme.com" else b""
        doc = Document(doc_id="h", source="feed_discovery", url=task.url, body=body, status=200)
        return FetchResult(True, 200, doc, False, None, 1)


def test_discover_writes_same_host_blog_feed(tmp_path):
    db = Database(tmp_path / "s.db")
    reg = AccountRegistry(db)
    acct = Account(domain="acme.com")
    reg.upsert(acct)
    fake = Fake()
    url = FeedDiscovery(fake, reg).discover(acct)
    assert url == "https://acme.com/blog/feed"
    assert reg.get("acme.com").blog_feed_url == url
    assert fake.urls[0] in {"https://acme.com/", "https://acme.com"}


def test_discover_skips_when_already_set():
    fake = Fake()
    acct = Account(domain="acme.com", blog_feed_url="https://acme.com/feed")
    assert FeedDiscovery(fake, None).discover(acct) is None
    assert fake.urls == []


def test_wayback_fallback_finds_feed_in_snapshot(tmp_path):
    """Live homepage is a JS shell; the archived snapshot exposes the feed."""
    import json as _json
    from src.core.db import Database
    from src.core.models import Account, Document
    from src.identity.feed_discovery import FeedDiscovery
    from src.identity.registry import AccountRegistry
    from src.core.http import FetchResult

    db = Database(tmp_path / "s.db")
    reg = AccountRegistry(db)
    acct = Account(domain="acme.com")
    reg.upsert(acct)

    cdx_body = _json.dumps([["timestamp", "original", "digest", "statuscode"],
                            ["20240101000000", "https://acme.com/", "d1", "200"]]).encode()
    snap_body = HTML.encode()  # fixture homepage with <link rel=alternate> to /blog/feed

    class WaybackFake:
        def __init__(self):
            self.urls = []

        def get(self, task, **kw):
            self.urls.append(task.url)
            if "web.archive.org/cdx" in (task.url or ""):
                body = cdx_body
            elif "web.archive.org/web/" in (task.url or ""):
                body = snap_body
            else:
                body = b""  # live homepage + feed guesses: all empty (JS shell)
            doc = Document(doc_id="h", source="feed_discovery", url=task.url, body=body, status=200)
            return FetchResult(True, 200, doc, False, None, 1)

    fake = WaybackFake()
    url = FeedDiscovery(fake, reg).discover(acct)
    assert url == "https://acme.com/blog/feed"
    assert reg.get("acme.com").blog_feed_url == url
    assert any("web.archive.org/cdx" in u for u in fake.urls)
    assert sum(1 for u in fake.urls if "web.archive.org/web/" in u) == 1


def test_wayback_fallback_ignores_third_party_feeds(tmp_path):
    """An archived page referencing a DIFFERENT host's feed must not be stored."""
    import json as _json
    from src.core.db import Database
    from src.core.models import Account, Document
    from src.identity.feed_discovery import FeedDiscovery
    from src.identity.registry import AccountRegistry
    from src.core.http import FetchResult

    db = Database(tmp_path / "s.db")
    reg = AccountRegistry(db)
    acct = Account(domain="acme.com")
    reg.upsert(acct)

    third_party_html = '<html><head><link rel="alternate" type="application/rss+xml" href="https://other-cdn.example/feed.xml"></head></html>'
    cdx_body = _json.dumps([["timestamp", "original", "digest", "statuscode"],
                            ["20240101000000", "https://acme.com/", "d1", "200"]]).encode()

    class WaybackFake:
        def get(self, task, **kw):
            if "web.archive.org/cdx" in (task.url or ""):
                body = cdx_body
            elif "web.archive.org/web/" in (task.url or ""):
                body = third_party_html.encode()
            else:
                body = b""
            doc = Document(doc_id="h", source="feed_discovery", url=task.url, body=body, status=200)
            return FetchResult(True, 200, doc, False, None, 1)

    assert FeedDiscovery(WaybackFake(), reg).discover(acct) is None
    assert reg.get("acme.com").blog_feed_url is None
