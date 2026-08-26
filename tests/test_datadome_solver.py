from unittest.mock import patch, MagicMock
from src.sources.techstack.datadome_solver import solve_datadome, TwoCaptchaDataDomeSolver


CAPTCHA_URL = "https://geo.captcha-delivery.com/captcha/?initialCid=AHrlqAAA&hash=ABC&cid=xyz&t=fe&referer=https%3A%2F%2Fwww.g2.com%2F&s=50168&e=abc"
PAGE_URL = "https://www.g2.com/products/databricks/reviews"


def test_solve_datadome_returns_cookie():
    """solve_datadome returns a datadome cookie string."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "errorId": 0,
        "taskId": "12345"
    }
    mock_result = MagicMock()
    mock_result.json.return_value = {
        "errorId": 0,
        "status": "ready",
        "solution": {
            "cookie": "datadome=abc123; Max-Age=31536000; Domain=.g2.com; Path=/; Secure; SameSite=Lax"
        }
    }
    with patch("httpx.Client.post", side_effect=[mock_response, mock_result]):
        cookie = solve_datadome(
            captcha_url=CAPTCHA_URL,
            page_url=PAGE_URL,
            provider="2captcha",
            api_key="fake_key",
            user_agent="Mozilla/5.0 Chrome/147",
            proxy="http://user:pass@proxy:8080",
        )
    assert cookie is not None
    assert "datadome=" in cookie
    assert "abc123" in cookie

def test_solve_datadome_returns_none_on_no_key():
    assert solve_datadome(
        captcha_url=CAPTCHA_URL, page_url=PAGE_URL,
        provider=None, api_key=None, user_agent="UA", proxy="direct"
    ) is None

def test_solve_datadome_rejects_banned_ip():
    """When t=bv in captcha_url, the IP is banned — don't even try."""
    banned_url = CAPTCHA_URL.replace("t=fe", "t=bv")
    result = solve_datadome(
        captcha_url=banned_url, page_url=PAGE_URL,
        provider="2captcha", api_key="fake", user_agent="UA", proxy="direct"
    )
    assert result is None

def test_two_captcha_solver_builds_task():
    """TwoCaptchaDataDomeSolver builds the correct task payload."""
    solver = TwoCaptchaDataDomeSolver(api_key="key123", user_agent="Chrome")
    task = solver._build_task(
        captcha_url=CAPTCHA_URL,
        page_url=PAGE_URL,
        proxy="http://user:pass@proxy:8080",
    )
    assert task["type"] == "DataDomeSliderTask"
    assert task["websiteURL"] == PAGE_URL
    assert task["captchaUrl"] == CAPTCHA_URL
    assert task["userAgent"] == "Chrome"
    assert task["proxyType"] == "http"
    assert task["proxyAddress"] == "proxy"
    assert task["proxyPort"] == 8080
