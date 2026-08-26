"""DataDome CAPTCHA solver adapters (2Captcha, CapSolver).

Unlike the Cloudflare solver (which returns a token), the DataDome solver
returns a `datadome` cookie value directly. The cookie is IP-bound and
User-Agent-bound — it must be used from the same proxy IP and with the
same User-Agent that solved the challenge.
"""
from __future__ import annotations

import time
from typing import Optional
from urllib.parse import urlparse


class TwoCaptchaDataDomeSolver:
    """2Captcha DataDome slider captcha solver.

    Submits a DataDomeSliderTask to 2Captcha and polls for the result.
    Returns the datadome cookie string on success, None on failure.
    """
    BASE = "https://api.2captcha.com"

    def __init__(self, api_key: str, user_agent: str | None = None, poll_interval: int = 5, max_poll: int = 24):
        self.api_key = api_key
        self.user_agent = user_agent
        self.poll_interval = poll_interval
        self.max_poll = max_poll

    def _build_task(self, *, captcha_url: str, page_url: str, proxy: str) -> dict:
        parsed = urlparse(proxy)
        proxy_type = parsed.scheme or "http"
        proxy_host = parsed.hostname or ""
        proxy_port = parsed.port or 8080
        task = {
            "type": "DataDomeSliderTask",
            "websiteURL": page_url,
            "captchaUrl": captcha_url,
            "userAgent": self.user_agent or "",
            "proxyType": proxy_type,
            "proxyAddress": proxy_host,
            "proxyPort": proxy_port,
        }
        if parsed.username:
            task["proxyLogin"] = parsed.username
        if parsed.password:
            task["proxyPassword"] = parsed.password
        return task

    def solve(self, *, captcha_url: str, page_url: str, proxy: str) -> str | None:
        import httpx

        task = self._build_task(captcha_url=captcha_url, page_url=page_url, proxy=proxy)
        body = {"clientKey": self.api_key, "task": task}
        r = httpx.Client(timeout=30).post(f"{self.BASE}/createTask", json=body)
        data = r.json()
        if data.get("errorId"):
            return None
        task_id = data.get("taskId")
        if not task_id:
            return None
        return self._poll(task_id)

    def _poll(self, task_id: str) -> str | None:
        import httpx

        body = {"clientKey": self.api_key, "taskId": task_id}
        for _ in range(self.max_poll):
            r = httpx.Client(timeout=30).post(f"{self.BASE}/getTaskResult", json=body)
            data = r.json()
            if data.get("status") == "ready":
                return data.get("solution", {}).get("cookie")
            time.sleep(self.poll_interval)
        return None


class CapSolverDataDomeSolver:
    """CapSolver DataDome slider captcha solver."""
    BASE = "https://api.capsolver.com"

    def __init__(self, api_key: str, user_agent: str | None = None, poll_interval: int = 3, max_poll: int = 40):
        self.api_key = api_key
        self.user_agent = user_agent
        self.poll_interval = poll_interval
        self.max_poll = max_poll

    def solve(self, *, captcha_url: str, page_url: str, proxy: str) -> str | None:
        import httpx

        parsed = urlparse(proxy)
        task = {
            "type": "DatadomeSliderTask",
            "websiteURL": page_url,
            "captchaUrl": captcha_url,
            "userAgent": self.user_agent or "",
            "proxyType": parsed.scheme or "http",
            "proxyAddress": parsed.hostname or "",
            "proxyPort": parsed.port or 8080,
        }
        if parsed.username:
            task["proxyLogin"] = parsed.username
        if parsed.password:
            task["proxyPassword"] = parsed.password
        body = {"clientKey": self.api_key, "task": task}
        r = httpx.Client(timeout=30).post(f"{self.BASE}/createTask", json=body)
        data = r.json()
        if data.get("errorId"):
            return None
        task_id = data.get("taskId")
        if not task_id:
            return None
        return self._poll(task_id)

    def _poll(self, task_id: str) -> str | None:
        import httpx

        body = {"clientKey": self.api_key, "taskId": task_id}
        for _ in range(self.max_poll):
            r = httpx.Client(timeout=30).post(f"{self.BASE}/getTaskResult", json=body)
            data = r.json()
            if data.get("status") == "ready":
                return data.get("solution", {}).get("cookie")
            time.sleep(self.poll_interval)
        return None


def solve_datadome(*, captcha_url: str, page_url: str, provider: str | None, api_key: str | None,
                  user_agent: str | None = None, proxy: str = "direct") -> str | None:
    """Solve a DataDome captcha and return the datadome cookie string.

    Returns None if no provider/api_key, or if t=bv (IP banned).
    """
    if not provider or not api_key:
        return None
    # Don't attempt if IP is banned (t=bv)
    if "t=bv" in captcha_url:
        return None
    if provider == "2captcha":
        return TwoCaptchaDataDomeSolver(api_key=api_key, user_agent=user_agent).solve(
            captcha_url=captcha_url, page_url=page_url, proxy=proxy)
    if provider == "capsolver":
        return CapSolverDataDomeSolver(api_key=api_key, user_agent=user_agent).solve(
            captcha_url=captcha_url, page_url=page_url, proxy=proxy)
    return None
