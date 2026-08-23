"""HAR-lite capture on BrowserFetcher. Dummy page — no Chromium."""

from __future__ import annotations

import json
from types import SimpleNamespace

from src.core.browser import BrowserFetcher
from src.core.config import BrowserConfig, Config
from src.core.db import Database
from src.core.rawstore import RawStore


class _DummyPage:
    def __init__(self, fired):
        self._handlers = []
        self._fired = fired

    def on(self, event, handler):
        if event == "request":
            self._handlers.append(handler)

    def remove_listener(self, event, handler):
        if event == "request" and handler in self._handlers:
            self._handlers.remove(handler)

    def goto(self, url, wait_until=None, timeout=None):
        for req in self._fired:
            for h in list(self._handlers):
                h(req)
        return SimpleNamespace(status=200)

    def content(self):
        return "<html></html>"

    def wait_for_timeout(self, ms):
        return None

    def wait_for_selector(self, sel):
        return None

    def evaluate(self, script):
        return None


def test_capture_network_stores_har_lite(tmp_path):
    cfg = Config()
    cfg.browser = BrowserConfig(enabled=True)
    db = Database(tmp_path / "s.db")
    store = RawStore(db, raw_dir=tmp_path / "raw")
    fetcher = BrowserFetcher(cfg, store)
    fired = [
        SimpleNamespace(url="https://cdn.segment.com/a.js", resource_type="script"),
        SimpleNamespace(url="https://acme.com/app.js?token=SECRET", resource_type="script"),
    ]
    fetcher._page = _DummyPage(fired)
    result = fetcher.fetch(
        "https://acme.com/",
        source="techstack",
        domain="acme.com",
        capture_network=True,
    )
    assert result.ok and result.doc is not None
    assert (result.doc.content_type or "").startswith("application/json")
    raw = json.loads(result.doc.body)
    hosts = {r["host"] for r in raw["requests"]}
    assert "cdn.segment.com" in hosts
    assert "acme.com" in hosts
    assert all("?" not in r["url"] for r in raw["requests"])
    assert raw["page_url"] == "https://acme.com/"
