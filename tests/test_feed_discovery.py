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
