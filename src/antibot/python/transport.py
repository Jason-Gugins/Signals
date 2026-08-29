"""Tier-1 anti-bot transport: native engine (real Chrome TLS + own h2).

SignalsTransport wraps the compiled signals_antibot engine (BoringSSL with
Chrome's native ClientHello behaviors + our own Chrome-byte-exact HTTP/2
stack). If the native engine is unavailable — not built, wrong platform —
it degrades gracefully to the existing CurlCffiFetcher tier.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field

from src.core.curl_fetcher import CurlCffiFetcher, CurlCffiResponse

# Chrome 151 posture — mirrors fingerprints.py CHROME_TARGET["major"] and the
# user_agent in tests/fixtures/antibot/chrome_fingerprint_reference.json.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)


@dataclass
class AntibotResponse:
    status: int
    body: bytes
    headers: dict
    cookies: dict = field(default_factory=dict)
    url: str = ""
    via: str = "tier1_native"   # tier1_native | tier1_curlcffi_fallback
    reused_connection: bool = False
    resumed_session: bool = False


def _engine():
    """Import the native engine, or None if unavailable (graceful tiering)."""
    try:
        import signals_antibot  # noqa: F401  (compiled module)

        return signals_antibot
    except ImportError:
        return None


def _headers_dict(pairs: list) -> dict:
    out: dict = {}
    for name, value in pairs:
        if name.startswith(":"):  # pseudo-headers are not real headers
            continue
        # repeat headers folded comma-style (h2 canonical)
        if name in out:
            out[name] = f"{out[name]}, {value}"
        else:
            out[name] = value
    return out


def _set_cookie_pairs(headers: dict) -> dict:
    cookies: dict = {}
    for raw in headers.get("set-cookie", "").split(", ") if headers.get("set-cookie") else []:
        if "=" in raw:
            name, _, value = raw.partition("=")
            cookies[name.strip()] = value.split(";")[0].strip()
    return cookies


class SignalsTransport:
    """Fast TLS tier. Fetches with the native engine; falls back to
    curl_cffi when the engine is unavailable (engine is optional at import)."""

    def __init__(self, user_agent: str = DEFAULT_USER_AGENT, proxy: str | None = None):
        self.user_agent = user_agent
        self.proxy = proxy
        self._engine = _engine()
        self._fallback = None  # lazily constructed CurlCffiFetcher

    @property
    def engine_available(self) -> bool:
        return self._engine is not None

    def fetch(
        self,
        url: str,
        *,
        headers: dict | None = None,
        cookies: list[dict] | None = None,
        method: str = "GET",
    ) -> AntibotResponse:
        if self._engine is not None:
            try:
                return self._fetch_native(url, headers=headers, cookies=cookies, method=method)
            except Exception:
                if not self._fallback_permitted():
                    raise
                # fall through to curl_cffi fallback
        return self._fetch_fallback(url, headers=headers, cookies=cookies, method=method)

    def _fallback_permitted(self) -> bool:
        # Allow the fallback by default; tests can disable it via attribute.
        return getattr(self, "allow_fallback", True)

    def _fetch_native(
        self,
        url: str,
        *,
        headers: dict | None,
        cookies: list[dict] | None,
        method: str,
    ) -> AntibotResponse:
        extra: list[tuple[str, str]] = []
        if headers:
            extra.extend(headers.items())
        if cookies:
            cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
            extra.append(("cookie", cookie_str))
        raw = self._engine.fetch_h2(url, extra)
        data = json.loads(raw)
        hdrs = _headers_dict(data["headers"])
        body = base64.b64decode(data.get("body_b64") or "")
        return AntibotResponse(
            status=data["status"],
            body=body,
            headers=hdrs,
            cookies=_set_cookie_pairs(hdrs),
            url=url,
            via="tier1_native",
        )

    def _fetch_fallback(
        self,
        url: str,
        *,
        headers: dict | None,
        cookies: list[dict] | None,
        method: str,
    ) -> AntibotResponse:
        if self._fallback is None:
            self._fallback = CurlCffiFetcher(user_agent=self.user_agent, proxy=self.proxy)
        resp: CurlCffiResponse = self._fallback.get(url, cookies=cookies, headers=headers)
        return AntibotResponse(
            status=resp.status,
            body=resp.body,
            headers=dict(resp.headers),
            cookies=dict(resp.cookies),
            url=url,
            via="tier1_curlcffi_fallback",
        )


__all__ = ["AntibotResponse", "SignalsTransport", "DEFAULT_USER_AGENT"]
