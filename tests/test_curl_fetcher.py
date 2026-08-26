from unittest.mock import patch, MagicMock
from src.core.curl_fetcher import CurlCffiFetcher


def test_curl_cffi_fetcher_impersonates_chrome():
    """CurlCffiFetcher uses impersonate='chrome' by default."""
    fetcher = CurlCffiFetcher(user_agent="Mozilla/5.0 Chrome/147")
    assert fetcher.impersonate == "chrome"


def test_curl_cffi_fetcher_get_returns_response():
    """CurlCffiFetcher.get returns a response with status, body, cookies."""
    fetcher = CurlCffiFetcher(user_agent="Mozilla/5.0 Chrome/147")
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = "<html>Real content</html>"
    mock_response.content = b"<html>Real content</html>"
    mock_response.cookies = {"datadome": "cookie_value"}
    with patch("src.core.curl_fetcher.curl_cffi_get", return_value=mock_response):
        resp = fetcher.get("https://example.com")
    assert resp.status == 200
    assert b"Real content" in resp.body
    assert resp.cookies.get("datadome") == "cookie_value"


def test_curl_cffi_fetcher_injects_cookies():
    """CurlCffiFetcher passes cookies as Cookie header."""
    fetcher = CurlCffiFetcher(user_agent="Mozilla/5.0 Chrome/147")
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = "<html>ok</html>"
    mock_response.content = b"<html>ok</html>"
    mock_response.cookies = {}
    with patch("src.core.curl_fetcher.curl_cffi_get", return_value=mock_response) as mock_get:
        fetcher.get("https://example.com", cookies=[{"name": "datadome", "value": "abc"}])
    call_kwargs = mock_get.call_args
    # The Cookie header should be set
    assert "datadome=abc" in str(call_kwargs)


def test_curl_cffi_fetcher_uses_proxy():
    """CurlCffiFetcher passes proxy to curl_cffi."""
    fetcher = CurlCffiFetcher(user_agent="Mozilla/5.0 Chrome/147", proxy="http://proxy:8080")
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = "ok"
    mock_response.content = b"ok"
    mock_response.cookies = {}
    with patch("src.core.curl_fetcher.curl_cffi_get", return_value=mock_response) as mock_get:
        fetcher.get("https://example.com")
    call_kwargs = mock_get.call_args
    assert "proxy" in str(call_kwargs) or "proxies" in str(call_kwargs)
