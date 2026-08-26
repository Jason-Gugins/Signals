from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from src.sources.techstack.datadome_bypass import DataDomeBypass, DataDomeOutcome
from src.core.http import FetchResult
from src.core.models import Document

CHALLENGE_BODY = b"""<html><script>var dd={'rt':'c','cid':'AHrlq','hsh':'ABC','t':'fe','s':50168,'e':'xyz','host':'geo.captcha-delivery.com','cookie':'abc'}</script>
<iframe src="https://geo.captcha-delivery.com/captcha/?initialCid=AHrlq&hash=ABC&cid=xyz&t=fe&referer=https%3A%2F%2Fwww.g2.com%2F&s=50168&e=xyz"></iframe></html>"""

REAL_BODY = b"<html><body><div class='paper'>Real G2 content</div></body></html>"


def _make_cfg(**overrides):
    defaults = dict(
        datadome=SimpleNamespace(
            enabled=True, bypass_strategy="solver",
            solver_provider="2captcha", solver_api_key="fake_key",
            headed_fallback=False, cookie_ttl_hours=24,
            residential_proxy=None,
        ),
        browser=SimpleNamespace(proxy_server=None, user_agent="Mozilla/5.0 Chrome/147"),
        http=SimpleNamespace(user_agent="Mozilla/5.0 Chrome/147"),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_datadome_bypass_cookie_reuse():
    """Tier 1: cached datadome cookie clears the challenge."""
    cookie_store = MagicMock()
    import json
    cookie_store.get.return_value = {"cookies": json.dumps([{"name": "datadome", "value": "cached_cookie"}])}

    curl_fetcher = MagicMock()
    curl_resp = MagicMock()
    curl_resp.status = 200
    curl_resp.body = REAL_BODY
    curl_resp.cookies = {}
    curl_fetcher.get.return_value = curl_resp

    bypass = DataDomeBypass(_make_cfg(), cookie_store, curl_fetcher)
    outcome = bypass.attempt(domain="g2.com", url="https://www.g2.com/products/slack/reviews",
                             user_agent="Mozilla/5.0 Chrome/147", proxy="direct")
    assert outcome.success is True
    assert outcome.method == "cookie_reuse"


def test_datadome_bypass_curl_cffi_clears():
    """Tier 2: curl_cffi TLS impersonation clears the challenge without a solver."""
    cookie_store = MagicMock()
    cookie_store.get.return_value = None

    curl_fetcher = MagicMock()
    curl_resp = MagicMock()
    curl_resp.status = 200
    curl_resp.body = REAL_BODY
    curl_resp.cookies = {"datadome": "new_cookie"}
    curl_fetcher.get.return_value = curl_resp

    bypass = DataDomeBypass(_make_cfg(), cookie_store, curl_fetcher)
    outcome = bypass.attempt(domain="g2.com", url="https://www.g2.com/products/slack/reviews",
                             user_agent="Mozilla/5.0 Chrome/147", proxy="direct")
    assert outcome.success is True
    assert outcome.method == "curl_cffi"


def test_datadome_bypass_solver_returns_cookie():
    """Tier 3: 2Captcha solver returns a datadome cookie."""
    cookie_store = MagicMock()
    cookie_store.get.return_value = None

    curl_fetcher = MagicMock()
    # First curl_cffi attempt returns a challenge body
    curl_resp1 = MagicMock()
    curl_resp1.status = 403
    curl_resp1.body = CHALLENGE_BODY
    curl_resp1.cookies = {}
    # Second curl_cffi attempt (with solver cookie) returns real content
    curl_resp2 = MagicMock()
    curl_resp2.status = 200
    curl_resp2.body = REAL_BODY
    curl_resp2.cookies = {"datadome": "solved_cookie"}
    curl_fetcher.get.side_effect = [curl_resp1, curl_resp2]

    bypass = DataDomeBypass(_make_cfg(), cookie_store, curl_fetcher)
    with patch("src.sources.techstack.datadome_bypass.solve_datadome", return_value="datadome=solved_cookie"):
        outcome = bypass.attempt(domain="g2.com", url="https://www.g2.com/products/slack/reviews",
                                 user_agent="Mozilla/5.0 Chrome/147", proxy="http://user:pass@proxy:8080")
    assert outcome.success is True
    assert outcome.method == "solver"
    # Cookie should be persisted
    cookie_store.put.assert_called_once()


def test_datadome_bypass_hard_stop_on_banned_ip():
    """When t=bv, the bypass should hard-stop immediately (no solver attempt)."""
    cookie_store = MagicMock()
    cookie_store.get.return_value = None

    # Set t=bv in BOTH the dd={} JS object (read by is_datadome_ip_banned) and the iframe URL.
    banned_body = CHALLENGE_BODY.replace(b"'t':'fe'", b"'t':'bv'").replace(b"t=fe", b"t=bv")

    curl_fetcher = MagicMock()
    curl_resp = MagicMock()
    curl_resp.status = 403
    curl_resp.body = banned_body
    curl_resp.cookies = {}
    curl_fetcher.get.return_value = curl_resp

    bypass = DataDomeBypass(_make_cfg(), cookie_store, curl_fetcher)
    with patch("src.sources.techstack.datadome_bypass.solve_datadome") as mock_solve:
        outcome = bypass.attempt(domain="g2.com", url="https://www.g2.com/products/slack/reviews",
                                 user_agent="Mozilla/5.0 Chrome/147", proxy="http://proxy:8080")
    assert outcome.success is False
    mock_solve.assert_not_called()  # Don't waste money solving when IP is banned


def test_datadome_bypass_disabled():
    """When datadome.enabled=False, the bypass returns failure immediately."""
    cfg = _make_cfg(datadome=SimpleNamespace(
        enabled=False, bypass_strategy="disabled",
        solver_provider=None, solver_api_key=None,
        headed_fallback=False, cookie_ttl_hours=24, residential_proxy=None,
    ))
    bypass = DataDomeBypass(cfg, MagicMock(), MagicMock())
    outcome = bypass.attempt(domain="g2.com", url="https://www.g2.com/products/slack/reviews",
                             user_agent="UA", proxy="direct")
    assert outcome.success is False


def test_datadome_bypass_stealth_browser_clears():
    """Tier 2.5: Patchright stealth browser clears the DataDome interstitial."""
    from types import SimpleNamespace
    cookie_store = MagicMock()
    cookie_store.get.return_value = None

    curl_fetcher = MagicMock()
    curl_resp = MagicMock()
    curl_resp.status = 403
    curl_resp.body = CHALLENGE_BODY  # rt='i' interstitial
    curl_resp.cookies = {}
    curl_fetcher.get.return_value = curl_resp

    stealth_browser = MagicMock()
    from src.core.http import FetchResult
    from src.core.models import Document
    stealth_doc = Document(doc_id="d", source="marketplace_g2",
                          url="https://www.g2.com/products/slack/reviews",
                          body=REAL_BODY)
    stealth_result = FetchResult(ok=True, status=200, doc=stealth_doc,
                                cached=False, error=None, elapsed_ms=5000)
    stealth_browser.fetch.return_value = stealth_result

    bypass = DataDomeBypass(_make_cfg(), cookie_store, curl_fetcher, stealth_browser=stealth_browser)
    outcome = bypass.attempt(domain="g2.com", url="https://www.g2.com/products/slack/reviews",
                             user_agent="Mozilla/5.0 Chrome/147", proxy="direct")
    assert outcome.success is True
    assert outcome.method == "stealth_browser"
    # Should have called stealth_browser.fetch with warmup_url
    call_kwargs = stealth_browser.fetch.call_args
    assert "warmup_url" in call_kwargs.kwargs or "warmup_url" in str(call_kwargs)


def test_datadome_bypass_stealth_browser_not_used_when_curl_cffi_clears():
    """Stealth browser is NOT called when curl_cffi already cleared the challenge."""
    cookie_store = MagicMock()
    cookie_store.get.return_value = None

    curl_fetcher = MagicMock()
    curl_resp = MagicMock()
    curl_resp.status = 200
    curl_resp.body = REAL_BODY
    curl_resp.cookies = {"datadome": "cookie"}
    curl_fetcher.get.return_value = curl_resp

    stealth_browser = MagicMock()

    bypass = DataDomeBypass(_make_cfg(), cookie_store, curl_fetcher, stealth_browser=stealth_browser)
    outcome = bypass.attempt(domain="g2.com", url="https://www.g2.com/products/slack/reviews",
                             user_agent="UA", proxy="direct")
    assert outcome.success is True
    assert outcome.method == "curl_cffi"
    stealth_browser.fetch.assert_not_called()
