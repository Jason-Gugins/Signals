"""TLS-impersonated HTTP fetcher using curl_cffi.

curl_cffi uses curl-impersonate under the hood to reproduce a real browser's
TLS/JA3/JA4 and HTTP/2 fingerprint. This clears the passive TLS-fingerprint
layer of DataDome (and Cloudflare) without needing a browser or CAPTCHA solver.

Usage:
    fetcher = CurlCffiFetcher(user_agent="Mozilla/5.0 Chrome/147")
    response = fetcher.get("https://www.g2.com/products/slack/reviews")
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit


def curl_cffi_get(url: str, **kwargs):
    """Thin wrapper around curl_cffi.requests.get.

    The import is deferred to call-time so that this module can be imported
    even when curl_cffi is not installed (e.g. when running tests with mocks).
    Tests patch this callable to inject mock responses.
    """
    from curl_cffi import requests as curl_requests

    return curl_requests.get(url, **kwargs)


@dataclass
class CurlCffiResponse:
    status: int
    body: bytes
    cookies: dict
    headers: dict
    url: str


class CurlCffiFetcher:
    def __init__(
        self,
        user_agent: str,
        impersonate: str = "chrome",
        proxy: str | None = None,
        cookie_jar=None,
    ):
        self.user_agent = user_agent
        self.impersonate = impersonate
        self.proxy = proxy
        self.cookie_jar = cookie_jar

    def get(self, url: str, *, cookies: list[dict] | None = None, headers: dict | None = None) -> CurlCffiResponse:
        all_headers = {"User-Agent": self.user_agent}
        if cookies:
            cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
            all_headers["Cookie"] = cookie_str
        elif self.cookie_jar is not None:
            # Cookie-jar hook (Task 23): emit stored cookies unless the caller
            # passed explicit cookies — caller wins.
            jar_cookies = self.cookie_jar.cookies_for(url)
            if jar_cookies:
                all_headers["Cookie"] = "; ".join(
                    f"{c['name']}={c['value']}" for c in jar_cookies
                )
        if headers:
            all_headers.update(headers)

        proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None

        r = curl_cffi_get(
            url,
            impersonate=self.impersonate,
            headers=all_headers,
            proxies=proxies,
            timeout=30,
        )

        cookie_dict = {}
        for name, value in r.cookies.items():
            cookie_dict[name] = value

        # Cookie-jar hook (Task 23): feed response cookies into the jar.
        if self.cookie_jar is not None and cookie_dict:
            host = (urlsplit(url).hostname or "").lower()
            for name, value in cookie_dict.items():
                try:
                    self.cookie_jar.update(name, value, host)
                except Exception:
                    pass

        return CurlCffiResponse(
            status=r.status_code,
            body=r.content,
            cookies=cookie_dict,
            headers=dict(r.headers),
            url=str(r.url),
        )
