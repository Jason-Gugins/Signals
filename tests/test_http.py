"""Tests for the polite HTTP fetcher. All HTTP mocked with respx."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import httpx
import respx

from src.core.config import Config
from src.core.db import Database
from src.core.http import HttpFetcher
from src.core.ratelimit import RateLimiter
from src.core.rawstore import RawStore
from src.core.runlog import RunContext


@dataclass
class Task:
    source: str
    url: str
    domain: Optional[str] = None
    method: str = "GET"
    headers: dict = field(default_factory=dict)
    json_body: Optional[dict] = None


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self):
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _fetcher(tmp_path, monkeypatch, clock=None, client=None):
    monkeypatch.setenv("SIGNALS_CONTACT_EMAIL", "ops@example.com")
    env = tmp_path / "empty.env"
    env.write_text("", encoding="utf-8")
    cfg = Config.load(env_path=env)
    db = Database(tmp_path / "signals.db")
    store = RawStore(db, raw_dir=tmp_path / "raw")
    clock = clock or FakeClock()
    limiter = RateLimiter(default_rate=100.0, clock=clock, sleep=clock.sleep)
    ctx = RunContext(db, stage="collect")
    ctx.__enter__()
    fetcher = HttpFetcher(
        cfg, store, limiter, ctx=ctx, client=client, sleep=clock.sleep, rng=lambda a, b: 0.0
    )
    return fetcher, store, ctx, db, clock


@respx.mock
def test_200_stores_document_and_logs(tmp_path, monkeypatch):
    url = "https://example.com/page"
    respx.get(url).mock(return_value=httpx.Response(200, content=b"hello", headers={"content-type": "text/plain"}))
    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
    fetcher, store, ctx, db, _ = _fetcher(tmp_path, monkeypatch)
    result = fetcher.get(Task(source="news_rss", url=url, domain="example.com"))
    assert result.ok is True
    assert result.status == 200
    assert result.doc is not None
    assert result.doc.body == b"hello"
    row = db.one("SELECT * FROM fetch_log WHERE run_id = ?", (ctx.run_id,))
    assert row["status"] == 200
    assert row["cached"] == 0
    ctx.__exit__(None, None, None)


@respx.mock
def test_304_cached_no_store_write(tmp_path, monkeypatch):
    url = "https://example.com/page"
    respx.get(url).mock(return_value=httpx.Response(304))
    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
    fetcher, store, ctx, db, _ = _fetcher(tmp_path, monkeypatch)
    result = fetcher.get(Task(source="news_rss", url=url), etag='"abc"')
    assert result.ok is True
    assert result.cached is True
    assert result.doc is None
    assert db.one("SELECT COUNT(*) AS n FROM documents")["n"] == 0
    ctx.__exit__(None, None, None)


@respx.mock
def test_500_retries_then_200(tmp_path, monkeypatch):
    url = "https://example.com/flaky"
    route = respx.get(url)
    route.side_effect = [
        httpx.Response(500),
        httpx.Response(500),
        httpx.Response(200, content=b"ok"),
    ]
    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
    clock = FakeClock()
    fetcher, store, ctx, db, clock = _fetcher(tmp_path, monkeypatch, clock=clock)
    result = fetcher.get(Task(source="news_rss", url=url))
    assert result.ok is True
    assert result.doc.body == b"ok"
    # backoff_base ** attempt + 0 jitter: 1.5 ** 1, 1.5 ** 2
    assert len(clock.sleeps) == 2
    assert abs(clock.sleeps[0] - 1.5) < 1e-9
    assert abs(clock.sleeps[1] - 2.25) < 1e-9
    row = db.one("SELECT error FROM fetch_log WHERE run_id = ?", (ctx.run_id,))
    assert row["error"] and "3" in row["error"]
    ctx.__exit__(None, None, None)


@respx.mock
def test_429_honours_retry_after_and_penalizes(tmp_path, monkeypatch):
    url = "https://example.com/slow"
    route = respx.get(url)
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "2"}),
        httpx.Response(200, content=b"ok"),
    ]
    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
    clock = FakeClock()
    fetcher, store, ctx, db, clock = _fetcher(tmp_path, monkeypatch, clock=clock)
    result = fetcher.get(Task(source="news_rss", url=url))
    assert result.ok is True
    assert 2.0 in clock.sleeps
    ctx.__exit__(None, None, None)


@respx.mock
def test_404_no_retry(tmp_path, monkeypatch):
    url = "https://example.com/missing"
    route = respx.get(url)
    route.mock(return_value=httpx.Response(404))
    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
    fetcher, store, ctx, db, clock = _fetcher(tmp_path, monkeypatch)
    result = fetcher.get(Task(source="news_rss", url=url))
    assert result.ok is False
    assert result.status == 404
    assert route.call_count == 1
    ctx.__exit__(None, None, None)


@respx.mock
def test_robots_disallow_private_allows_public(tmp_path, monkeypatch):
    robots = "User-agent: *\nDisallow: /private\nAllow: /public\n"
    respx.get("https://example.com/robots.txt").mock(
        return_value=httpx.Response(200, text=robots)
    )
    respx.get("https://example.com/public").mock(return_value=httpx.Response(200, content=b"ok"))
    fetcher, store, ctx, db, _ = _fetcher(tmp_path, monkeypatch)
    blocked = fetcher.get(Task(source="news_rss", url="https://example.com/private"))
    assert blocked.ok is False
    assert blocked.error and "robots" in blocked.error.lower()
    allowed = fetcher.get(Task(source="news_rss", url="https://example.com/public"))
    assert allowed.ok is True
    ctx.__exit__(None, None, None)


@respx.mock
def test_ua_contains_contact_email(tmp_path, monkeypatch):
    url = "https://example.com/ua"
    route = respx.get(url).mock(return_value=httpx.Response(200, content=b"ok"))
    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
    fetcher, store, ctx, db, _ = _fetcher(tmp_path, monkeypatch)
    fetcher.get(Task(source="news_rss", url=url))
    sent = route.calls[0].request.headers["user-agent"]
    assert "ops@example.com" in sent
    ctx.__exit__(None, None, None)


@respx.mock
def test_get_json_non_json_returns_none(tmp_path, monkeypatch):
    url = "https://example.com/notjson"
    respx.get(url).mock(return_value=httpx.Response(200, content=b"not json"))
    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
    fetcher, store, ctx, db, _ = _fetcher(tmp_path, monkeypatch)
    result, payload = fetcher.get_json(Task(source="news_rss", url=url))
    assert result.ok is True
    assert payload is None
    ctx.__exit__(None, None, None)


# Observed 2026-08-24: news.google.com/robots.txt is Disallow: / for User-agent: *
# with Allow only for /, /home, /topics/, etc. — not /rss/.
_GNEWS_ROBOTS = """User-agent: *
Disallow: /
Allow: /$
Allow: /?
Allow: /home$
Allow: /topics/
"""


@respx.mock
def test_google_news_rss_allowed_despite_robots_disallow_all(tmp_path, monkeypatch):
    rss = "https://news.google.com/rss/search?q=Acme"
    blocked = "https://news.google.com/swg/extra"
    respx.get("https://news.google.com/robots.txt").mock(
        return_value=httpx.Response(200, text=_GNEWS_ROBOTS)
    )
    respx.get(rss).mock(return_value=httpx.Response(200, content=b"<rss/>"))
    respx.get(blocked).mock(return_value=httpx.Response(200, content=b"nope"))
    fetcher, store, ctx, db, _ = _fetcher(tmp_path, monkeypatch)
    ok = fetcher.get(Task(source="google_news", url=rss, domain="acme.com"))
    assert ok.ok is True
    assert ok.status == 200
    denied = fetcher.get(Task(source="google_news", url=blocked, domain="acme.com"))
    assert denied.ok is False
    assert denied.error and "robots" in denied.error.lower()
    ctx.__exit__(None, None, None)


def test_robots_path_allowed_only_prefixed_host():
    from src.core.http import robots_path_allowed

    allow = [{"host": "news.google.com", "path_prefix": "/rss/"}]
    assert robots_path_allowed("https://news.google.com/rss/search?q=x", allow)
    assert robots_path_allowed("https://news.google.com/rss/headlines/section/topic/TECHNOLOGY", allow)
    assert not robots_path_allowed("https://news.google.com/home", allow)
    assert not robots_path_allowed("https://www.bing.com/news/search?q=x", allow)
