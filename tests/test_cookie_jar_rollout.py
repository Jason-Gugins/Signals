"""Task 23: core-side persistent cookie-jar rollout to http/curl/browser fetchers.

Covers: jar merge order (caller-wins), persistence round-trip, per-scope
files, and disabled-by-default behavior (no jar kwarg -> byte-identical
fetch behavior, no Cookie header emitted).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.core.cookiejar import PersistentCookieJar


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNALS_CONTACT_EMAIL", "ops@example.com")
    env = tmp_path / "empty.env"
    env.write_text("", encoding="utf-8")
    from src.core.config import Config

    return Config.load(env_path=env)


def _cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNALS_CONTACT_EMAIL", "ops@example.com")
    env = tmp_path / "empty.env"
    env.write_text("", encoding="utf-8")
    from src.core.config import Config

    return Config.load(env_path=env)


# ---------------------------------------------------------------------------
# Jar: caller-wins merge + persistence + per-scope files
# ---------------------------------------------------------------------------


class TestJarBasics:
    def test_persistence_round_trip(self, tmp_path: Path):
        p = tmp_path / "cookies" / "http.json"
        jar = PersistentCookieJar(path=p)
        jar.update("sid", "abc123", "example.com", expires=None)
        jar.save()

        jar2 = PersistentCookieJar(path=p)
        jar2.load()
        got = jar2.cookies_for("https://www.example.com/page")
        assert got == [{"name": "sid", "value": "abc123", "domain": "example.com"}]

    def test_load_tolerates_missing_file(self, tmp_path: Path):
        jar = PersistentCookieJar(path=tmp_path / "nope" / "x.json")
        jar.load()  # must not raise
        assert jar.cookies_for("https://a.com") == []

    def test_load_tolerates_corrupt_file(self, tmp_path: Path):
        p = tmp_path / "x.json"
        p.write_text("{not json", encoding="utf-8")
        jar = PersistentCookieJar(path=p)
        jar.load()  # must not raise
        assert jar.cookies_for("https://a.com") == []

    def test_per_scope_files(self, tmp_path: Path):
        for scope in ("http", "curl", "browser"):
            jar = PersistentCookieJar(scope=scope, root=tmp_path / "cookies")
            jar.update("sid", f"v-{scope}", "example.com")
            jar.save()
            f = tmp_path / "cookies" / f"{scope}.json"
            assert f.exists()
            data = json.loads(f.read_text(encoding="utf-8"))
            assert data[0]["value"] == f"v-{scope}"

    def test_default_path_under_data_cookies(self):
        jar = PersistentCookieJar(scope="http")
        assert str(jar.path).replace("\\", "/").endswith("data/cookies/http.json")

    def test_expired_cookies_not_emitted(self, tmp_path: Path):
        t = 1000.0
        jar = PersistentCookieJar(path=tmp_path / "c.json", clock=lambda: t)
        jar.update("old", "v", "example.com", expires=t - 1)
        assert jar.cookies_for("https://example.com") == []


class TestMergeOrder:
    def test_explicit_caller_cookie_wins_over_stored_http(self, tmp_path: Path, monkeypatch):
        """HttpFetcher: task.headers Cookie must override the jar's stored cookie."""
        pytest.importorskip("httpx")
        from src.core.http import HttpFetcher
        from src.sources.base import FetchTask
        from src.core.cookiejar import PersistentCookieJar

        jar = PersistentCookieJar(path=tmp_path / "c.json")
        jar.update("sid", "stored", "example.com")

        captured = {}

        class FakeResp:
            status_code = 200
            content = b"<html>ok</html>"
            headers = {"content-type": "text/html", "etag": None, "last-modified": None}

        class FakeClient:
            def get(self, url):
                import httpx

                raise httpx.ConnectError("no robots")

            def request(self, method, url, **kwargs):
                captured["headers"] = kwargs["headers"]
                return FakeResp()

        class FakeStore:
            def put(self, **kw):
                class D:
                    body = b"<html>ok</html>"
                return D()

        class FakeLimiter:
            def wait(self, url):
                pass

            def slot(self):
                import contextlib

                return contextlib.nullcontext()

        fetcher = HttpFetcher(
            _cfg(tmp_path, monkeypatch), FakeStore(), FakeLimiter(), client=FakeClient(), cookie_jar=jar
        )
        task = FetchTask(
            source="t", url="https://example.com/x",
            headers={"Cookie": "sid=caller"},
        )
        result = fetcher.get(task)
        assert result.ok
        cookie_header = captured["headers"]["Cookie"]
        assert cookie_header == "sid=caller"

    def test_jar_emits_cookie_when_no_caller_cookie(self, tmp_path: Path, monkeypatch):
        pytest.importorskip("httpx")
        from src.core.http import HttpFetcher
        from src.sources.base import FetchTask
        from src.core.cookiejar import PersistentCookieJar

        jar = PersistentCookieJar(path=tmp_path / "c.json")
        jar.update("sid", "stored", "example.com")

        captured = {}

        class FakeResp:
            status_code = 200
            content = b"<html>ok</html>"
            headers = {"content-type": "text/html"}

        class FakeClient:
            def get(self, url):
                import httpx

                raise httpx.ConnectError("no robots")

            def request(self, method, url, **kwargs):
                captured["headers"] = kwargs["headers"]
                return FakeResp()

        class FakeStore:
            def put(self, **kw):
                class D:
                    body = b"<html>ok</html>"
                return D()

        class FakeLimiter:
            def wait(self, url):
                pass

            def slot(self):
                import contextlib

                return contextlib.nullcontext()

        fetcher = HttpFetcher(
            _cfg(tmp_path, monkeypatch), FakeStore(), FakeLimiter(), client=FakeClient(), cookie_jar=jar
        )
        task = FetchTask(source="t", url="https://example.com/x")
        result = fetcher.get(task)
        assert result.ok
        assert captured["headers"]["Cookie"] == "sid=stored"

    def test_response_set_cookie_fed_into_jar(self, tmp_path: Path, monkeypatch):
        pytest.importorskip("httpx")
        from src.core.http import HttpFetcher
        from src.sources.base import FetchTask
        from src.core.cookiejar import PersistentCookieJar

        jar = PersistentCookieJar(path=tmp_path / "c.json")

        class FakeResp:
            status_code = 200
            content = b"<html>ok</html>"
            headers = {"content-type": "text/html", "set-cookie": "fresh=respval; Domain=example.com; Path=/"}

        class FakeClient:
            def get(self, url):
                import httpx

                raise httpx.ConnectError("no robots")

            def request(self, method, url, **kwargs):
                return FakeResp()

        class FakeStore:
            def put(self, **kw):
                class D:
                    body = b"<html>ok</html>"
                return D()

        class FakeLimiter:
            def wait(self, url):
                pass

            def slot(self):
                import contextlib

                return contextlib.nullcontext()

        fetcher = HttpFetcher(
            _cfg(tmp_path, monkeypatch), FakeStore(), FakeLimiter(), client=FakeClient(), cookie_jar=jar
        )
        task = FetchTask(source="t", url="https://example.com/x")
        result = fetcher.get(task)
        assert result.ok
        got = jar.cookies_for("https://example.com/y")
        assert {"name": "fresh", "value": "respval", "domain": "example.com"} in got


class TestDisabledByDefault:
    def test_http_no_jar_kwarg_no_cookie_header(self, tmp_path: Path, monkeypatch):
        """Without cookie_jar, request headers carry no Cookie key at all."""
        pytest.importorskip("httpx")
        from src.core.http import HttpFetcher
        from src.sources.base import FetchTask

        captured = {}

        class FakeResp:
            status_code = 200
            content = b"<html>ok</html>"
            headers = {"content-type": "text/html"}

        class FakeClient:
            def get(self, url):
                import httpx

                raise httpx.ConnectError("no robots")

            def request(self, method, url, **kwargs):
                captured["headers"] = kwargs["headers"]
                return FakeResp()

        class FakeStore:
            def put(self, **kw):
                class D:
                    body = b"<html>ok</html>"
                return D()

        class FakeLimiter:
            def wait(self, url):
                pass

            def slot(self):
                import contextlib

                return contextlib.nullcontext()

        fetcher = HttpFetcher(_cfg(tmp_path, monkeypatch), FakeStore(), FakeLimiter(), client=FakeClient())
        task = FetchTask(source="t", url="https://example.com/x")
        result = fetcher.get(task)
        assert result.ok
        assert "Cookie" not in captured["headers"]

    def test_curl_no_jar_kwarg_no_cookie_header(self):
        """Without jar, CurlCffiFetcher builds no Cookie header."""
        from unittest.mock import patch

        import src.core.curl_fetcher as cf
        from src.core.curl_fetcher import CurlCffiFetcher, CurlCffiResponse

        captured = {}

        def fake_get(url, **kwargs):
            captured["headers"] = kwargs["headers"]
            return type("R", (), {"status_code": 200, "content": b"x", "cookies": {}, "headers": {}, "url": url})()

        with patch.object(cf, "curl_cffi_get", fake_get):
            f = CurlCffiFetcher(user_agent="UA")
            resp = f.get("https://example.com/x")
        assert isinstance(resp, CurlCffiResponse)
        assert "Cookie" not in captured["headers"]

    def test_http_behavior_identical_without_jar(self):
        """Byte-identical: default kwargs reproduce existing constructor signature."""
        import inspect
        from src.core.http import HttpFetcher

        sig = inspect.signature(HttpFetcher.__init__)
        params = sig.parameters
        assert "cookie_jar" in params
        assert params["cookie_jar"].default is None
        # cookie_jar is keyword-with-default; existing positional order intact
        names = list(params)
        assert names[:5] == ["self", "config", "store", "limiter", "ctx"]


class TestCurlJarHook:
    def test_explicit_cookies_win_over_jar(self, tmp_path: Path):
        from unittest.mock import patch

        import src.core.curl_fetcher as cf
        from src.core.curl_fetcher import CurlCffiFetcher
        from src.core.cookiejar import PersistentCookieJar

        jar = PersistentCookieJar(path=tmp_path / "c.json")
        jar.update("sid", "stored", "example.com")

        captured = {}

        def fake_get(url, **kwargs):
            captured["headers"] = kwargs["headers"]
            return type("R", (), {"status_code": 200, "content": b"x", "cookies": {}, "headers": {}, "url": url})()

        with patch.object(cf, "curl_cffi_get", fake_get):
            f = CurlCffiFetcher(user_agent="UA", cookie_jar=jar)
            f.get("https://example.com/x", cookies=[{"name": "sid", "value": "explicit"}])
        assert captured["headers"]["Cookie"] == "sid=explicit"

    def test_jar_emitted_and_response_fed(self, tmp_path: Path):
        from unittest.mock import patch

        import src.core.curl_fetcher as cf
        from src.core.curl_fetcher import CurlCffiFetcher
        from src.core.cookiejar import PersistentCookieJar

        jar = PersistentCookieJar(path=tmp_path / "c.json")
        jar.update("sid", "stored", "example.com")

        captured = []

        def fake_get(url, **kwargs):
            captured.append(kwargs["headers"])
            return type(
                "R", (),
                {
                    "status_code": 200, "content": b"x",
                    "cookies": {"fresh": "rv"}, "headers": {}, "url": url,
                },
            )()

        with patch.object(cf, "curl_cffi_get", fake_get):
            f = CurlCffiFetcher(user_agent="UA", cookie_jar=jar)
            f.get("https://example.com/x")
            f.get("https://example.com/y")

        # First call: jar cookie emitted; header contains stored cookie
        assert "sid=stored" in captured[0]["Cookie"]
        # Response cookie was fed into the jar and emitted on second call
        assert "fresh=rv" in captured[1]["Cookie"]


class TestBrowserJarHook:
    def test_browser_accepts_jar_kwarg(self):
        """BrowserFetcher constructor accepts optional cookie_jar (default None)."""
        import inspect
        from src.core.browser import BrowserFetcher

        sig = inspect.signature(BrowserFetcher.__init__)
        assert "cookie_jar" in sig.parameters
        assert sig.parameters["cookie_jar"].default is None

    def test_solve_cookies_captured_into_jar(self, tmp_path: Path):
        """capture_html path feeds solve_context.cookies() into the jar."""
        from src.core.browser import BrowserFetcher
        from src.core.cookiejar import PersistentCookieJar

        jar = PersistentCookieJar(path=tmp_path / "c.json")

        class FakePage:
            def goto(self, *a, **kw):
                pass

            def wait_for_timeout(self, ms):
                pass

            def title(self):
                return "Real Page"

            def content(self):
                return "<html>done</html>"

            def on(self, *a):
                pass

            def query_selector_all(self, sel):
                return []

            def evaluate(self, *a, **kw):
                return None

        class FakeSolveCtx:
            def __init__(self):
                pass

            def add_init_script(self, *a):
                pass

            def new_page(self):
                return FakePage()

            def cookies(self):
                return [{"name": "cf_clearance", "value": "tok", "domain": ".example.com", "path": "/"}]

            def close(self):
                pass

        class FakeBrowser:
            def new_context(self, **kw):
                return FakeSolveCtx()

        class FakeStore:
            def put(self, **kw):
                class D:
                    body = b"<html>done</html>"
                return D()

        class FakeCfg:
            class browser:
                enabled = True
                viewport = {"width": 1280, "height": 720}
                locale = "en-US"
                timezone = "UTC"
                user_agent = "UA"
                proxy_server = None
                session_dir = str(tmp_path / "sess")
                min_delay = 0.0
                max_delay = 0.0

            class cloudflare:
                solve_timeout_ms = 100

        bf = BrowserFetcher(FakeCfg(), FakeStore(), cookie_jar=jar)
        bf._browser = FakeBrowser()
        bf._context = FakeSolveCtx()
        bf._page = FakePage()

        result = bf.fetch("https://example.com/x", source="t", domain="example.com", capture_html=True)
        assert result.ok
        got = jar.cookies_for("https://example.com/y")
        assert {"name": "cf_clearance", "value": "tok", "domain": ".example.com"} in got

    def test_new_context_seeded_from_jar(self, tmp_path: Path):
        """When the jar is enabled, new contexts get jar.cookies_for(url) added."""
        from src.core.browser import BrowserFetcher
        from src.core.cookiejar import PersistentCookieJar

        jar = PersistentCookieJar(path=tmp_path / "c.json")
        jar.update("cf_clearance", "tok", ".example.com")

        seeded = {}

        class FakePage:
            def goto(self, *a, **kw):
                pass

            def wait_for_timeout(self, ms):
                pass

            def title(self):
                return "Real Page"

            def content(self):
                return "<html>done</html>"

            def query_selector_all(self, sel):
                return []

        class FakeSolveCtx:
            def add_init_script(self, *a):
                pass

            def new_page(self):
                return FakePage()

            def add_cookies(self, cookies):
                seeded.update({c["name"]: c for c in cookies})

            def cookies(self):
                return []

            def close(self):
                pass

        class FakeBrowser:
            def new_context(self, **kw):
                return FakeSolveCtx()

        class FakeStore:
            def put(self, **kw):
                class D:
                    body = b"<html>done</html>"
                return D()

        class FakeCfg:
            class browser:
                enabled = True
                viewport = {"width": 1280, "height": 720}
                locale = "en-US"
                timezone = "UTC"
                user_agent = "UA"
                proxy_server = None
                session_dir = str(tmp_path / "sess")
                min_delay = 0.0
                max_delay = 0.0

            class cloudflare:
                solve_timeout_ms = 100

        bf = BrowserFetcher(FakeCfg(), FakeStore(), cookie_jar=jar)
        bf._browser = FakeBrowser()
        bf._context = FakeSolveCtx()
        bf._page = FakePage()

        result = bf.fetch("https://example.com/x", source="t", domain="example.com", capture_html=True)
        assert result.ok
        assert seeded.get("cf_clearance", {}).get("value") == "tok"

    def test_no_jar_no_seeding(self, tmp_path: Path):
        """Without jar, new contexts never receive add_cookies from a jar."""
        from src.core.browser import BrowserFetcher

        calls = {"add_cookies": 0}

        class FakePage:
            def goto(self, *a, **kw):
                pass

            def wait_for_timeout(self, ms):
                pass

            def title(self):
                return "Real Page"

            def content(self):
                return "<html>done</html>"

            def query_selector_all(self, sel):
                return []

        class FakeSolveCtx:
            def add_init_script(self, *a):
                pass

            def new_page(self):
                return FakePage()

            def add_cookies(self, cookies):
                calls["add_cookies"] += 1

            def cookies(self):
                return []

            def close(self):
                pass

        class FakeBrowser:
            def new_context(self, **kw):
                return FakeSolveCtx()

        class FakeStore:
            def put(self, **kw):
                class D:
                    body = b"<html>done</html>"
                return D()

        class FakeCfg:
            class browser:
                enabled = True
                viewport = {"width": 1280, "height": 720}
                locale = "en-US"
                timezone = "UTC"
                user_agent = "UA"
                proxy_server = None
                session_dir = str(tmp_path / "sess")
                min_delay = 0.0
                max_delay = 0.0

            class cloudflare:
                solve_timeout_ms = 100

        bf = BrowserFetcher(FakeCfg(), FakeStore())
        bf._browser = FakeBrowser()
        bf._context = FakeSolveCtx()
        bf._page = FakePage()

        result = bf.fetch("https://example.com/x", source="t", domain="example.com", capture_html=True)
        assert result.ok
        assert calls["add_cookies"] == 0
