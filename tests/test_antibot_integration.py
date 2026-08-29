"""Integration tests: SignalsShadow as preferred tier-1 in bypass waterfalls.

Task 6 of the antibot plan. All shadows are fakes — no network, no native
engine required.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.antibot.python.transport import AntibotResponse
from src.core.config import Config
from src.sources.techstack.cf_bypass import CloudflareBypass
from src.sources.techstack.datadome_bypass import DataDomeBypass


# --- fakes ---------------------------------------------------------------

CHALLENGE_BODY = (
    b"<html><head><title>Attention Required</title></head>"
    b"<body>datadome challenge</body></html>"
)
OK_BODY = b"<html><body>real page content</body></html>"


class FakeShadow:
    """Stands in for SignalsTransport. Returns canned AntibotResponses."""

    def __init__(self, *, status: int = 200, body: bytes = OK_BODY, cookies=None):
        self.status = status
        self.body = body
        self.cookies = cookies or {}
        self.calls: list[dict] = []

    def fetch(self, url, *, headers=None, cookies=None, method="GET", conditional=None):
        self.calls.append({"url": url, "cookies": cookies, "headers": headers})
        return AntibotResponse(
            status=self.status, body=self.body, headers={}, cookies=dict(self.cookies),
            url=url,
        )


class FakeCookieStore:
    def __init__(self):
        self.rows = {}

    def get(self, domain, *, user_agent=None, proxy=None):
        return self.rows.get(domain)

    def put(self, domain, *, user_agent, proxy, cookies, expires_at, solve_method):
        self.rows[domain] = {
            "cookies": cookies, "user_agent": user_agent, "proxy": proxy,
            "expires_at": expires_at, "solve_method": solve_method,
        }

    def clear(self, domain):
        self.rows.pop(domain, None)


class FakeCurl:
    def __init__(self, *, status: int = 200, body: bytes = OK_BODY, cookies=None):
        self.status = status
        self.body = body
        self.cookies = cookies or {}
        self.calls = []

    def get(self, url, *, cookies=None, headers=None):
        self.calls.append({"url": url, "cookies": cookies})
        return SimpleNamespace(status=self.status, body=self.body, cookies=dict(self.cookies))


class FakeBrowser:
    def __init__(self):
        self.calls = []

    def fetch(self, url, **kw):
        self.calls.append({"url": url, **kw})
        return SimpleNamespace(ok=False, status=403, doc=None, cloudflare_cookies=[])


# --- fixtures ------------------------------------------------------------

@pytest.fixture
def dd_config():
    cfg = Config()
    cfg.datadome.enabled = True
    cfg.datadome.bypass_strategy = "solver"
    cfg.datadome.solver_provider = None
    cfg.datadome.solver_api_key = None
    return cfg


@pytest.fixture
def cf_config():
    cfg = Config()
    cfg.cloudflare.enabled = True
    cfg.cloudflare.bypass_strategy = "browser_first"
    return cfg


# --- DataDome waterfall ----------------------------------------------------

def test_datadome_bypass_prefers_shadow_when_enabled(dd_config):
    """Shadow present + clean response -> success via signals_shadow; no curl/browser calls."""
    curl = FakeCurl()
    browser = FakeBrowser()
    shadow = FakeShadow(status=200, body=OK_BODY, cookies={"datadome": "tok123"})
    bypass = DataDomeBypass(dd_config, FakeCookieStore(), curl,
                            stealth_browser=browser, shadow=shadow)
    out = bypass.attempt(domain="g2.com", url="https://www.g2.com/products/x/reviews",
                         user_agent="UA", proxy="direct")
    assert out.success is True
    assert out.method == "signals_shadow"
    assert len(shadow.calls) == 1
    assert curl.calls == []
    assert browser.calls == []
    # datadome cookie persisted via the cookie store
    store = bypass.cookies
    assert "g2.com" in store.rows
    assert store.rows["g2.com"]["solve_method"] == "signals_shadow"
    assert any(c["name"] == "datadome" for c in store.rows["g2.com"]["cookies"])


def test_datadome_bypass_falls_back_to_curl_when_shadow_absent(dd_config):
    """shadow=None -> existing behavior (curl_cffi tier used)."""
    curl = FakeCurl(status=200, body=OK_BODY)
    bypass = DataDomeBypass(dd_config, FakeCookieStore(), curl, stealth_browser=None)
    out = bypass.attempt(domain="g2.com", url="https://www.g2.com/products/x/reviews",
                         user_agent="UA", proxy="direct")
    assert out.success is True
    assert out.method == "curl_cffi"
    assert len(curl.calls) == 1


def test_datadome_bypass_falls_back_when_shadow_fails(dd_config):
    """Shadow hit by challenge -> waterfall continues to curl_cffi."""
    curl = FakeCurl(status=200, body=OK_BODY)
    shadow = FakeShadow(status=403, body=CHALLENGE_BODY)
    bypass = DataDomeBypass(dd_config, FakeCookieStore(), curl, shadow=shadow)
    out = bypass.attempt(domain="g2.com", url="https://www.g2.com/products/x/reviews",
                         user_agent="UA", proxy="direct")
    assert out.success is True
    assert out.method == "curl_cffi"
    assert len(shadow.calls) == 1
    assert len(curl.calls) >= 1


def test_datadome_shadow_challenge_still_escalates_when_curl_also_challenged(dd_config):
    """Shadow challenge + curl challenge -> reaches solver/hard-stop path (no crash)."""
    curl = FakeCurl(status=403, body=CHALLENGE_BODY)
    shadow = FakeShadow(status=403, body=CHALLENGE_BODY)
    bypass = DataDomeBypass(dd_config, FakeCookieStore(), curl, shadow=shadow)
    out = bypass.attempt(domain="g2.com", url="https://www.g2.com/products/x/reviews",
                         user_agent="UA", proxy="direct")
    assert out.success is False
    assert len(shadow.calls) == 1


# --- Cloudflare waterfall --------------------------------------------------

def _cf_fetch_result(body: bytes, status: int = 200, cookies=None):
    from src.core.http import FetchResult
    from src.core.models import Document

    doc = Document(doc_id="d1", source="techstack", url="https://example.com/", body=body)
    return FetchResult(ok=status == 200, status=status, doc=doc, cached=False,
                       error=None, elapsed_ms=0, cloudflare_cookies=cookies or [])


class FakeCfHttp:
    """http_fetcher for CloudflareBypass (cookie-replay tier)."""

    def __init__(self):
        self.calls = []

    def replay(self, url, *, cookies, user_agent):
        self.calls.append({"url": url, "cookies": cookies})
        return _cf_fetch_result(b"<html>challenge</html>", status=403)


class FakeCfBrowser:
    def __init__(self):
        self.calls = []

    def fetch(self, url, **kw):
        self.calls.append({"url": url, **kw})
        return _cf_fetch_result(b"<html>cleared</html>", cookies=[{"name": "cf_clearance", "value": "v", "expires": 0}])


def test_cf_bypass_prefers_shadow_before_browser(cf_config):
    """Shadow present + clean response -> signals_shadow; browser tier not reached."""
    browser = FakeCfBrowser()
    shadow = FakeShadow(status=200, body=OK_BODY, cookies={"cf_clearance": "abc"})
    store = FakeCookieStore()
    bypass = CloudflareBypass(cf_config, store, FakeCfHttp(), browser, shadow=shadow)
    out = bypass.attempt(domain="example.com", url="https://example.com/tech",
                         user_agent="UA", proxy="direct")
    assert out.success is True
    assert out.method == "signals_shadow"
    assert len(shadow.calls) == 1
    assert browser.calls == []
    assert "example.com" in store.rows
    assert store.rows["example.com"]["solve_method"] == "signals_shadow"


def test_cf_bypass_falls_back_to_browser_when_shadow_absent(cf_config):
    """shadow=None -> existing behavior (browser tier used)."""
    browser = FakeCfBrowser()
    bypass = CloudflareBypass(cf_config, FakeCookieStore(), FakeCfHttp(), browser)
    out = bypass.attempt(domain="example.com", url="https://example.com/tech",
                         user_agent="UA", proxy="direct")
    assert out.success is True
    assert out.method == "browser"
    assert len(browser.calls) == 1


def test_cf_bypass_falls_back_when_shadow_challenged(cf_config):
    """Shadow challenge -> browser tier runs next."""
    browser = FakeCfBrowser()
    shadow = FakeShadow(status=403, body=b"<html>Just a moment...</html>")
    bypass = CloudflareBypass(cf_config, FakeCookieStore(), FakeCfHttp(), browser, shadow=shadow)
    out = bypass.attempt(domain="example.com", url="https://example.com/tech",
                         user_agent="UA", proxy="direct")
    assert out.success is True
    assert out.method == "browser"
    assert len(shadow.calls) == 1


# --- config ---------------------------------------------------------------

def test_antibot_config_loads():
    cfg = Config.load("config/default.yaml")
    assert isinstance(cfg.antibot.enabled, bool)
    assert isinstance(cfg.antibot.fallback, str)
    assert cfg.antibot.fallback == "curl_cffi"
    assert cfg.antibot.recheck_after_hours == 24
