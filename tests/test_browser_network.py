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


def test_har_prefers_late_third_party_host(tmp_path):
    cfg = Config()
    cfg.browser = BrowserConfig(enabled=True)
    db = Database(tmp_path / "s.db")
    store = RawStore(db, raw_dir=tmp_path / "raw")
    fetcher = BrowserFetcher(cfg, store)
    fired = [
        SimpleNamespace(url=f"https://acme.com/asset/{i}.css", resource_type="stylesheet")
        for i in range(90)
    ]
    fired.append(SimpleNamespace(url="https://cdn.cookielaw.org/scripttemplates/otSDKStub.js", resource_type="script"))
    fetcher._page = _DummyPage(fired)
    result = fetcher.fetch("https://acme.com/", source="techstack", domain="acme.com", capture_network=True)
    raw = json.loads(result.doc.body)
    hosts = [r["host"] for r in raw["requests"]]
    assert "cdn.cookielaw.org" in hosts
    assert len(raw["requests"]) <= 80


class _ChallengePage(_DummyPage):
    """Simulates a Cloudflare JS challenge that clears after a poll."""
    def __init__(self, fired, cleared_body):
        super().__init__(fired)
        self._cleared = cleared_body
        self._polls = 0
    def title(self):
        return "Just a moment..." if self._polls < 2 else "Acme Corp"
    def content(self):
        return self._cleared if self._polls >= 2 else "<html><title>Just a moment...</title></html>"
    def wait_for_timeout(self, ms):
        self._polls += 1
    def cookies(self):
        if self._polls < 2:
            return []
        return [{"name": "cf_clearance", "value": "tok", "domain": "acme.com"},
                {"name": "__cf_bm", "value": "bm1", "domain": ".acme.com"}]


def test_browser_fetch_solves_js_challenge_returns_html(tmp_path):
    cfg = Config()
    cfg.browser = BrowserConfig(enabled=True)
    db = Database(tmp_path / "s.db")
    store = RawStore(db, raw_dir=tmp_path / "raw")
    fetcher = BrowserFetcher(cfg, store)
    fetcher._page = _ChallengePage(fired=[], cleared_body="<html><script src='https://js.hs-scripts.com/123.js'></script></html>")
    fetcher._context = type("C", (), {"cookies": lambda self: fetcher._page.cookies()})()
    result = fetcher.fetch("https://acme.com/", source="techstack", domain="acme.com", capture_html=True)
    assert result.ok
    assert result.doc is not None
    assert b"hs-scripts.com" in result.doc.body
    assert result.cloudflare_cookies
    names = {c["name"] for c in result.cloudflare_cookies}
    assert "cf_clearance" in names
    assert "__cf_bm" in names  # full jar, not just cf_clearance


def test_build_context_args_skips_storage_state(tmp_path):
    """When skip_storage_state=True, session.json is NOT loaded even if it exists."""
    cfg = Config()
    cfg.browser = BrowserConfig(enabled=True, session_dir=str(tmp_path))
    # Create a stale session.json
    (tmp_path / "session.json").write_text('{"cookies": [], "origins": []}')
    args_default = BrowserFetcher._build_context_args(cfg)
    args_fresh = BrowserFetcher._build_context_args(cfg, skip_storage_state=True)
    assert "storage_state" in args_default  # loads session by default
    assert "storage_state" not in args_fresh  # skips session when asked


def test_new_solve_context_skips_storage_state(tmp_path):
    """_new_solve_context creates a context without storage_state."""
    cfg = Config()
    cfg.browser = BrowserConfig(enabled=True, session_dir=str(tmp_path))
    (tmp_path / "session.json").write_text('{"cookies": [], "origins": []}')
    fetcher = BrowserFetcher(cfg, RawStore(Database(tmp_path / "s.db"), raw_dir=tmp_path / "raw"))

    # Mock browser: track what new_context receives
    created_args = []
    class _MockBrowser:
        def new_context(self, **kwargs):
            created_args.append(kwargs)
            ctx = type("Ctx", (), {
                "add_init_script": lambda self, s: None,
                "new_page": lambda self: _DummyPage(fired=[]),
                "cookies": lambda self: [],
                "close": lambda self: None,
            })()
            return ctx
    fetcher._browser = _MockBrowser()

    ctx, page = fetcher._new_solve_context()
    assert len(created_args) == 1
    assert "storage_state" not in created_args[0]
    assert page is not None
    ctx.close()


def test_capture_html_uses_fresh_context_when_browser_running(tmp_path):
    """When self._browser is set, capture_html creates a fresh solve context
    instead of reusing the main one (which may have stale storage_state)."""
    cfg = Config()
    cfg.browser = BrowserConfig(enabled=True, session_dir=str(tmp_path))
    fetcher = BrowserFetcher(cfg, RawStore(Database(tmp_path / "s.db"), raw_dir=tmp_path / "raw"))

    solve_called = []
    challenge_page = _ChallengePage(
        fired=[], cleared_body="<html><script src='https://js.hs-scripts.com/1.js'></script></html>"
    )
    solve_ctx = type("Ctx", (), {
        "add_init_script": lambda self, s: None,
        "new_page": lambda self: challenge_page,
        "cookies": lambda self: challenge_page.cookies(),
        "close": lambda self: None,
    })()

    def mock_new_solve_context():
        solve_called.append(True)
        return solve_ctx, challenge_page

    fetcher._new_solve_context = mock_new_solve_context
    fetcher._browser = object()  # truthy — simulates real browser running
    # Prevent fetch() from calling self.start() -> a REAL Playwright launch
    # (CI runners have no chromium binary installed). Same stub as the
    # sibling test_capture_html_does_not_pollute_session_json.
    fetcher._page = _DummyPage(fired=[])

    result = fetcher.fetch(
        "https://acme.com/", source="techstack", domain="acme.com",
        capture_html=True,
    )
    assert solve_called  # fresh context was used
    assert result.ok
    assert result.cloudflare_cookies  # cookies extracted from solve context


def test_capture_html_falls_back_to_stubs_when_no_browser(tmp_path):
    """When self._browser is None (test mode), capture_html uses the existing
    self._page / self._context stubs — backward compat with existing tests."""
    cfg = Config()
    cfg.browser = BrowserConfig(enabled=True)
    fetcher = BrowserFetcher(cfg, RawStore(Database(tmp_path / "s.db"), raw_dir=tmp_path / "raw"))
    fetcher._page = _ChallengePage(
        fired=[], cleared_body="<html><script src='https://js.hs-scripts.com/1.js'></script></html>"
    )
    fetcher._context = type("C", (), {"cookies": lambda self: fetcher._page.cookies()})()
    # self._browser is None — should use stubs, not _new_solve_context
    result = fetcher.fetch(
        "https://acme.com/", source="techstack", domain="acme.com",
        capture_html=True,
    )
    assert result.ok
    assert b"hs-scripts.com" in result.doc.body


def test_capture_html_does_not_pollute_session_json(tmp_path):
    """After a capture_html solve with a real browser, the main context
    (and thus session.json via save_state()) must NOT contain cf_clearance —
    the solve used a fresh context that was closed."""
    cfg = Config()
    cfg.browser = BrowserConfig(enabled=True, session_dir=str(tmp_path))
    session_file = tmp_path / "session.json"
    session_file.write_text('{"cookies": [{"name": "cf_clearance", "value": "STALE"}], "origins": []}')

    fetcher = BrowserFetcher(cfg, RawStore(Database(tmp_path / "s.db"), raw_dir=tmp_path / "raw"))

    challenge_page = _ChallengePage(
        fired=[], cleared_body="<html><script src='https://js.hs-scripts.com/1.js'></script></html>"
    )
    solve_ctx = type("Ctx", (), {
        "add_init_script": lambda self, s: None,
        "new_page": lambda self: challenge_page,
        "cookies": lambda self: challenge_page.cookies(),
        "close": lambda self: None,
    })()
    fetcher._new_solve_context = lambda: (solve_ctx, challenge_page)
    fetcher._browser = object()  # real browser running
    fetcher._page = _DummyPage(fired=[])  # prevent start() from launching real Playwright

    # Main context (the one save_state reads) — should stay clean
    main_cookies = []
    fetcher._context = type("C", (), {"cookies": lambda self: main_cookies})

    result = fetcher.fetch(
        "https://acme.com/", source="techstack", domain="acme.com",
        capture_html=True,
    )
    assert result.ok
    assert result.cloudflare_cookies  # solve context had cookies
    # Main context never got CF cookies — save_state would write main_cookies
    assert not any(c.get("name") == "cf_clearance" for c in main_cookies)
