"""Cloudflare challenge solver adapters (optional, external services).

IMPORTANT: These solvers return a Turnstile *token* (a cf-turnstile-response
form-field value), NOT a cf_clearance cookie. The caller (cf_bypass.py) must
re-enter a browser context, inject the token into the Turnstile callback, and
let Cloudflare's edge set the real cf_clearance cookie. There is no stateless
token→cookie exchange via httpx.
"""
from __future__ import annotations
import time
from typing import Protocol, Optional
import httpx


class Solver(Protocol):
    def solve(self, *, url: str, sitekey: str | None, user_agent: str | None = None) -> str | None: ...


class NoSolver:
    def solve(self, *, url, sitekey, user_agent=None):
        return None


class TwoCaptchaSolver:
    """2Captcha Cloudflare Turnstile/Challenge solver.

    Returns a Turnstile token (solution.token) — a form-field value, NOT a
    cf_clearance cookie. See module docstring.

    Task types:
      - TurnstileTaskProxyless: first-party Turnstile (site owner embedded widget, has sitekey)
      - AntiCloudflareTaskProxyless: CF's own managed interstitial (no sitekey, URL only)
    """
    BASE = "https://api.2captcha.com"

    def __init__(self, api_key: str, user_agent: str | None = None, poll_interval: int = 5, max_poll: int = 24):
        self.api_key = api_key
        self.user_agent = user_agent
        self.poll_interval = poll_interval
        self.max_poll = max_poll

    def submit(self, *, url: str, sitekey: str | None, user_agent: str | None) -> str:
        # First-party Turnstile (has sitekey) vs CF managed interstitial (no sitekey)
        task_type = "TurnstileTaskProxyless" if sitekey else "AntiCloudflareTaskProxyless"
        body = {
            "clientKey": self.api_key,
            "task": {
                "type": task_type,
                "websiteURL": url,
                "websiteKey": sitekey or "",
                "userAgent": user_agent or self.user_agent or "",
            },
        }
        r = httpx.Client(timeout=30).post(f"{self.BASE}/createTask", json=body)
        return r.json().get("request") or r.json().get("taskId")

    def fetch(self, task_id: str) -> str | None:
        body = {"clientKey": self.api_key, "taskId": task_id}
        for _ in range(self.max_poll):
            r = httpx.Client(timeout=30).post(f"{self.BASE}/getTaskResult", json=body)
            data = r.json()
            if data.get("status") == "ready":
                return data.get("solution", {}).get("token") or data.get("request")
            time.sleep(self.poll_interval)
        return None

    def solve(self, *, url: str, sitekey: str | None, user_agent: str | None = None) -> str | None:
        task_id = self.submit(url=url, sitekey=sitekey, user_agent=user_agent)
        return self.fetch(task_id)


def solve_cloudflare(*, url: str, sitekey: str | None, provider: str | None, api_key: str | None, user_agent: str | None = None) -> str | None:
    if not provider or not api_key:
        return None
    if provider == "2captcha":
        return TwoCaptchaSolver(api_key=api_key, user_agent=user_agent).solve(url=url, sitekey=sitekey, user_agent=user_agent)
    if provider == "anticaptcha":
        s = TwoCaptchaSolver(api_key=api_key, user_agent=user_agent)
        s.BASE = "https://api.anti-captcha.com"
        return s.solve(url=url, sitekey=sitekey, user_agent=user_agent)
    return None
