"""Tier-1 anti-bot transport: native engine (real Chrome TLS + own h2).

SignalsTransport wraps the compiled signals_antibot engine (BoringSSL with
Chrome's native ClientHello behaviors + our own Chrome-byte-exact HTTP/2
stack). If the native engine is unavailable — not built, wrong platform —
it degrades gracefully to the existing CurlCffiFetcher tier.

Task 3 (temporal stealth): when the engine exposes SignalsEngine, fetches go
through a persistent per-origin state (TLS session resumption + h2 connection
pooling) and responses are conditionally revalidated (If-None-Match /
If-Modified-Since → 304 → cached body, from_cache=True).
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field

from src.core.curl_fetcher import CurlCffiFetcher, CurlCffiResponse

from .cookies import PersistentCookieJar
from .temporal import RevalidationCache

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
    from_cache: bool = False


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
    curl_cffi when the engine is unavailable (engine is optional at import).

    Conditionally revalidates previously-seen GET URLs (Chrome's cache
    behavior): second fetch of an URL with an ETag/Last-Modified sends the
    validators and serves the cached body on 304 with ``from_cache=True``.
    """

    def __init__(
        self,
        user_agent: str = DEFAULT_USER_AGENT,
        proxy: str | None = None,
        conditional: bool = True,
        cookie_jar: "PersistentCookieJar | None" = None,
    ):
        self.user_agent = user_agent
        self.proxy = proxy
        self.conditional = conditional
        self.cookie_jar = cookie_jar
        self._engine = _engine()
        self._native = None  # persistent SignalsEngine (pool + session store)
        self._cache = RevalidationCache()
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
        conditional: bool | None = None,
    ) -> AntibotResponse:
        do_conditional = self.conditional if conditional is None else conditional
        # Merge the jar's cookies under the caller's (caller wins on name
        # conflicts) — Chrome always re-sends stored cookies.
        if self.cookie_jar is not None:
            jar_cookies = self.cookie_jar.cookies_for(url)
            provided = {c["name"]: c for c in cookies or []}
            for jc in jar_cookies:
                if jc["name"] not in provided:
                    provided[jc["name"]] = jc
            cookies = list(provided.values()) or None
        response = self._fetch(
            url, headers=headers, cookies=cookies, method=method,
            conditional=do_conditional,
        )
        if self.cookie_jar is not None:
            self.cookie_jar.set_from_response(url, response.headers)
        if do_conditional and method == "GET":
            self._cache.on_response(url, response)
        return response

    def _fetch(
        self,
        url: str,
        *,
        headers: dict | None,
        cookies: list[dict] | None,
        method: str,
        conditional: bool,
    ) -> AntibotResponse:
        # Conditional revalidation (Chrome cache semantics, GET only).
        merged_headers = dict(headers or {})
        if conditional and method == "GET" and self._cache.has_validators(url):
            merged_headers.update(self._cache.conditional_headers(url))

        if self._engine is not None:
            try:
                resp = self._fetch_native(
                    url, headers=merged_headers, cookies=cookies, method=method
                )
                return self._maybe_serve_cached(url, resp, conditional)
            except Exception:
                if not self._fallback_permitted():
                    raise
                # fall through to curl_cffi fallback
        resp = self._fetch_fallback(url, headers=merged_headers, cookies=cookies, method=method)
        return self._maybe_serve_cached(url, resp, conditional)

    def _maybe_serve_cached(
        self, url: str, resp: AntibotResponse, conditional: bool
    ) -> AntibotResponse:
        """A 304 for a URL we hold a cached body for → serve the cache."""
        if conditional and resp.status == 304:
            try:
                return self._cache.serve_304(url, resp)
            except KeyError:
                pass  # no entry (evicted/no validators) — surface the 304
        return resp

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
        if method != "GET":
            raise ValueError(f"native engine supports GET only (got {method})")

        # Persistent per-origin state (session resumption + h2 pooling).
        if self._native is None:
            if hasattr(self._engine, "SignalsEngine"):
                self._native = self._engine.SignalsEngine()
            else:
                self._native = False  # old engine: fall back to free function
        if self._native is False:
            raw = self._engine.fetch_h2(url, extra)
        else:
            raw = self._native.fetch(url, extra)

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
            reused_connection=bool(data.get("reused_connection", False)),
            resumed_session=bool(data.get("resumed_session", False)),
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
