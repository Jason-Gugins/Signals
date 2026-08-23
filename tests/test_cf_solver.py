import pytest
from src.sources.techstack.cf_solver import NoSolver, TwoCaptchaSolver, solve_cloudflare


def test_no_solver_returns_none():
    assert NoSolver().solve(url="https://acme.com", sitekey=None) is None


def test_twocaptcha_solver_builds_request(monkeypatch):
    calls = {}
    def fake_post(self, url, json=None):
        calls["url"] = url
        calls["body"] = json
        class R:
            status_code = 200
            def json(self_inner): return {"request": "TASKID123"}
        return R()
    monkeypatch.setattr("httpx.Client.post", fake_post)
    s = TwoCaptchaSolver(api_key="key", user_agent="UA")
    task_id = s.submit(url="https://acme.com", sitekey="0xabc", user_agent="UA")
    assert task_id == "TASKID123"
    assert "2captcha" in calls["url"].lower() or "rucaptcha" in calls["url"].lower()


def test_solve_cloudflare_returns_none_without_config():
    # No solver configured → returns None (no network)
    assert solve_cloudflare(url="https://acme.com", sitekey=None, provider=None, api_key=None) is None
