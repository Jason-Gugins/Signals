"""Mock-level tests for Turnstile token -> cf_clearance injection.

Mirrors tests/test_cf_solver_wiring.py: no live browser, no live solver.
inject_turnstile_token(browser, url, token) must navigate, set the cookie,
reload, and poll for challenge clearance — returning bool, never raising.
Live validation (real 2Captcha key + real challenge) is explicitly out of scope.
"""
import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _make_browser(title="Example Domain", content="<html>real content</html>"):
    page = MagicMock()
    page.title.return_value = title
    page.content.return_value = content
    context = MagicMock()
    browser = SimpleNamespace(_page=page, _context=context)
    return browser, page, context


# --- unit level: inject_turnstile_token ---


def test_inject_turnstile_token_signature():
    from src.core.browser import inject_turnstile_token

    sig = inspect.signature(inject_turnstile_token)
    assert sig.parameters["timeout_ms"].default == 20000
    assert sig.parameters["token"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD


def test_inject_sets_cf_clearance_cookie_with_domain_and_path():
    from src.core.browser import inject_turnstile_token

    browser, page, context = _make_browser()
    ok = inject_turnstile_token(browser, "https://example.com/challenge", "tok123")
    assert ok is True
    cookies = context.add_cookies.call_args[0][0]
    assert len(cookies) == 1
    c = cookies[0]
    assert c["name"] == "cf_clearance"
    assert c["value"] == "tok123"
    assert c["domain"] == "example.com"
    assert c["path"] == "/"


def test_inject_navigates_and_reloads():
    from src.core.browser import inject_turnstile_token

    browser, page, context = _make_browser()
    ok = inject_turnstile_token(browser, "https://example.com/challenge", "tok123")
    assert ok is True
    assert page.goto.call_count == 1
    page.goto.assert_called_once_with("https://example.com/challenge", wait_until="domcontentloaded")
    assert page.reload.call_count == 1


def test_inject_returns_false_on_timeout_without_clearing():
    from src.core.browser import inject_turnstile_token

    browser, page, context = _make_browser(title="Just a moment...")
    ok = inject_turnstile_token(browser, "https://example.com/challenge", "tok123",
                                timeout_ms=1, poll_ms=1)
    assert ok is False


def test_inject_never_raises_on_navigation_error():
    from src.core.browser import inject_turnstile_token

    browser, page, context = _make_browser()
    page.goto.side_effect = RuntimeError("net::ERR_CONNECTION_REFUSED")
    ok = inject_turnstile_token(browser, "https://example.com/challenge", "tok123")
    assert ok is False


# --- wiring level: cf_bypass tier 3 end-to-end (mocked) ---


def _make_bypass(browser, *, headed_fallback=False):
    from src.sources.techstack.cf_bypass import CloudflareBypass

    cfg = SimpleNamespace(
        cloudflare=SimpleNamespace(
            enabled=True, bypass_strategy="solver_first",
            solver_provider="2captcha", solver_api_key="fake",
            headed_fallback=headed_fallback, solve_timeout_ms=5000,
            cookie_ttl_hours=24,
        ),
        browser=SimpleNamespace(proxy_server=None),
    )
    http = MagicMock()
    cookie_store = MagicMock()
    cookie_store.get.return_value = None
    return CloudflareBypass(cfg, cookie_store, http, browser)


def test_tier3_inject_success_returns_solver_outcome():
    from src.core.http import FetchResult
    from src.core.models import Document

    browser, page, context = _make_browser()
    doc = Document(doc_id="b", source="techstack", url="https://example.com", body=b"<html>ok</html>")
    browser.fetch = MagicMock(return_value=FetchResult(True, 200, doc, False, None, 1, []))
    context.cookies.return_value = [
        {"name": "cf_clearance", "value": "tok123", "domain": ".example.com", "path": "/"},
        {"name": "__cf_bm", "value": "x", "domain": ".example.com", "path": "/"},
    ]
    page.content.return_value = "<html>real content</html>"
    bypass = _make_bypass(browser)
    with patch("src.sources.techstack.cf_bypass.solve_cloudflare", return_value="tok123"), \
         patch("src.sources.techstack.cf_bypass.inject_turnstile_token", return_value=True) as inj, \
         patch.object(bypass, "_extract_turnstile_sitekey", return_value="0x1234"):
        outcome = bypass.attempt(domain="example.com", url="https://example.com", user_agent="UA")
    inj.assert_called_once()
    assert outcome.success is True
    assert outcome.method == "solver"
    assert outcome.result is not None and outcome.result.ok
    names = {c["name"] for c in outcome.cookies}
    assert "cf_clearance" in names


def test_tier3_inject_failure_falls_through_to_tier4_headed():
    from src.core.http import FetchResult
    from src.core.models import Document

    browser, page, context = _make_browser()
    doc = Document(doc_id="b", source="techstack", url="https://example.com", body=b"<html>ok</html>")
    headed = FetchResult(True, 200, doc, False, None, 1,
                         [{"name": "cf_clearance", "value": "manual", "domain": ".example.com", "path": "/"}])
    # tier 2 browser fetch fails; tier 4 headed solve succeeds
    browser.fetch = MagicMock(side_effect=[FetchResult(False, 403, None, False, "challenge", 1, []), headed])
    bypass = _make_bypass(browser, headed_fallback=True)
    with patch("src.sources.techstack.cf_bypass.solve_cloudflare", return_value="tok123"), \
         patch("src.sources.techstack.cf_bypass.inject_turnstile_token", return_value=False), \
         patch.object(bypass, "_extract_turnstile_sitekey", return_value="0x1234"):
        outcome = bypass.attempt(domain="example.com", url="https://example.com", user_agent="UA")
    assert outcome.success is True
    assert outcome.method == "headed"


def test_tier3_inject_false_no_headed_fallback_yields_failure():
    from unittest.mock import patch as p
    from src.core.http import FetchResult

    browser, page, context = _make_browser()
    browser.fetch = MagicMock(
        return_value=FetchResult(False, 403, None, False, "challenge", 1, []))
    bypass = _make_bypass(browser, headed_fallback=False)
    with p("src.sources.techstack.cf_bypass.solve_cloudflare", return_value="tok123"), \
         p("src.sources.techstack.cf_bypass.inject_turnstile_token", return_value=False), \
         p.object(bypass, "_extract_turnstile_sitekey", return_value="0x1234"):
        outcome = bypass.attempt(domain="example.com", url="https://example.com", user_agent="UA")
    assert outcome.success is False
    assert outcome.method is None
