import inspect
from src.core.browser import BrowserFetcher

def test_browser_fetch_accepts_inject_turnstile_token():
    """BrowserFetcher.fetch() accepts inject_turnstile_token kwarg."""
    sig = inspect.signature(BrowserFetcher.fetch)
    assert "inject_turnstile_token" in sig.parameters

def test_solver_via_browser_passes_token():
    """_solver_via_browser calls browser.fetch with inject_turnstile_token."""
    from unittest.mock import MagicMock, patch
    from types import SimpleNamespace
    from src.sources.techstack.cf_bypass import CloudflareBypass
    from src.core.http import FetchResult
    from src.core.models import Document

    cfg = SimpleNamespace(
        cloudflare=SimpleNamespace(
            enabled=True, bypass_strategy="browser_first",
            solver_provider="2captcha", solver_api_key="fake",
            headed_fallback=False, solve_timeout_ms=5000, cookie_ttl_hours=24,
        ),
        browser=SimpleNamespace(proxy_server=None),
    )
    browser = MagicMock()
    doc = Document(doc_id="b", source="techstack", url="https://example.com", body=b"<html>ok</html>")
    browser.fetch.return_value = FetchResult(True, 200, doc, False, None, 1, [])
    http = MagicMock()
    cookie_store = MagicMock()
    cookie_store.get.return_value = None
    bypass = CloudflareBypass(cfg, cookie_store, http, browser)
    with patch("src.sources.techstack.cf_bypass.solve_cloudflare", return_value="fake_token"):
        with patch.object(bypass, "_extract_turnstile_sitekey", return_value="0x1234"):
            browser.fetch.side_effect = [
                FetchResult(True, 200,
                    Document(doc_id="c", source="techstack", url="https://example.com",
                             body=b"<html><div class='cf-turnstile' data-sitekey='0x1234'></div></html>"),
                    False, None, 1, []),
                FetchResult(True, 200, doc, False, None, 1, []),
            ]
            outcome = bypass.attempt(domain="example.com", url="https://example.com", user_agent="UA")
    calls = browser.fetch.call_args_list
    assert any("inject_turnstile_token" in str(c) for c in calls)
