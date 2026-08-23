"""Polite HTTP fetcher: UA, conditional GET, retries, robots, raw-store hand-off."""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

from src.core.config import Config
from src.core.models import Document
from src.core.ratelimit import RateLimiter
from src.core.rawstore import RawStore
from src.core.runlog import RunContext

RETRY_STATUSES = {429, 500, 502, 503, 504}


@dataclass
class FetchResult:
    ok: bool
    status: int
    doc: Optional[Document]
    cached: bool
    error: Optional[str]
    elapsed_ms: int
    cloudflare_cookies: list = field(default_factory=list)


class RobotsCache:
    def __init__(self, client: httpx.Client, user_agent: str):
        self.client = client
        self.user_agent = user_agent
        self._parsers: dict[str, RobotFileParser | None] = {}

    def allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        key = f"{parsed.scheme}://{parsed.netloc}".lower()
        if key not in self._parsers:
            rp = RobotFileParser()
            robots_url = urljoin(key + "/", "robots.txt")
            try:
                resp = self.client.get(robots_url)
                if resp.status_code >= 400:
                    self._parsers[key] = None
                else:
                    rp.parse(resp.text.splitlines())
                    self._parsers[key] = rp
            except httpx.HTTPError:
                self._parsers[key] = None
        parser = self._parsers[key]
        if parser is None:
            return True
        return parser.can_fetch(self.user_agent, url)


class HttpFetcher:
    def __init__(
        self,
        config: Config,
        store: RawStore,
        limiter: RateLimiter,
        ctx: RunContext | None = None,
        client: httpx.Client | None = None,
        sleep=time.sleep,
        rng=random.uniform,
    ):
        self.config = config
        self.store = store
        self.limiter = limiter
        self.ctx = ctx
        self._owns_client = client is None
        if client is not None:
            self.client = client
        else:
            client_kwargs: dict[str, Any] = dict(
                timeout=config.http.timeout_seconds,
                verify=config.http.verify_tls,
                follow_redirects=True,
            )
            proxy_url = (
                getattr(config.browser, "proxy_server", None)
                if hasattr(config, "browser") and config.browser.proxy_server
                else None
            )
            if proxy_url:
                client_kwargs["proxy"] = proxy_url
            self.client = httpx.Client(**client_kwargs)
        self.sleep = sleep
        self.rng = rng
        self._robots: RobotsCache | None = None

    def _ua(self) -> str:
        return self.config.resolved_user_agent()

    def _robots_cache(self) -> RobotsCache:
        if self._robots is None:
            self._robots = RobotsCache(self.client, self._ua())
        return self._robots

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def get(
        self,
        task,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchResult:
        return self._request(task, etag=etag, last_modified=last_modified)

    def replay(self, url: str, *, cookies: list[dict], user_agent: str) -> FetchResult:
        """Replay an HTTP GET with a full stored cookie jar + matching UA.

        Builds a FetchTask with Cookie+UA headers and delegates to ``get``.
        Used by CloudflareBypass._replay_with_cookies for the cookie-reuse tier.
        """
        from src.sources.base import FetchTask

        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        task = FetchTask(
            source="techstack",
            url=url,
            headers={"Cookie": cookie_str, "User-Agent": user_agent},
            meta={"kind": "html"},
        )
        return self.get(task)

    def get_json(self, task, **kw) -> tuple[FetchResult, Any]:
        result = self.get(task, **kw)
        if not result.ok or result.doc is None or not result.doc.body:
            return result, None
        try:
            return result, json.loads(result.doc.body)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
            return result, None

    def post_json(self, task, **kw) -> FetchResult:
        return self._request(task, etag=None, last_modified=None)

    def _request(self, task, *, etag: str | None, last_modified: str | None) -> FetchResult:
        url = task.url
        method = getattr(task, "method", "GET") or "GET"
        started = time.monotonic()
        if self.config.http.respect_robots and method.upper() == "GET":
            if not self._robots_cache().allowed(url):
                result = FetchResult(
                    ok=False, status=0, doc=None, cached=False,
                    error="robots disallowed", elapsed_ms=0,
                )
                self._log(task, result, attempts=1)
                return result

        headers = {
            "User-Agent": self._ua(),
            "Accept-Encoding": "gzip, deflate",
        }
        extra = getattr(task, "headers", None) or {}
        headers.update(extra)
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        max_retries = self.config.http.max_retries
        backoff_base = self.config.http.backoff_base
        last_error = None
        status = 0
        attempts = 0
        response: httpx.Response | None = None

        for attempt in range(0, max_retries + 1):
            attempts = attempt + 1
            self.limiter.wait(url)
            try:
                with self.limiter.slot():
                    kwargs: dict[str, Any] = {"headers": headers}
                    body = getattr(task, "json_body", None)
                    if body is not None:
                        kwargs["json"] = body
                    response = self.client.request(method, url, **kwargs)
                status = response.status_code
                if status == 304:
                    result = FetchResult(
                        ok=True, status=304, doc=None, cached=True,
                        error=None, elapsed_ms=_elapsed(started),
                    )
                    self._log(task, result, attempts=attempts)
                    return result
                if 200 <= status < 300:
                    doc = self.store.put(
                        source=getattr(task, "source", "http"),
                        url=url,
                        body=response.content,
                        content_type=response.headers.get("content-type"),
                        status=status,
                        domain=getattr(task, "domain", None),
                        etag=response.headers.get("etag"),
                        last_modified=response.headers.get("last-modified"),
                    )
                    result = FetchResult(
                        ok=True, status=status, doc=doc, cached=False,
                        error=_attempts_note(attempts), elapsed_ms=_elapsed(started),
                    )
                    self._log(task, result, attempts=attempts)
                    return result
                if status in RETRY_STATUSES and attempt < max_retries:
                    if status in {429, 503}:
                        self.limiter.penalize(url, seconds=5.0)
                    self._backoff(attempt + 1, response)
                    last_error = f"HTTP {status}"
                    continue
                # Store 403 bodies so the Cloudflare bypass can classify them.
                # (Cloudflare challenges return HTML even on 403.)
                doc = None
                if status == 403 and response.content:
                    doc = self.store.put(
                        source=getattr(task, "source", "http"),
                        url=url,
                        body=response.content,
                        content_type=response.headers.get("content-type"),
                        status=status,
                        domain=getattr(task, "domain", None),
                    )
                result = FetchResult(
                    ok=False, status=status, doc=doc, cached=False,
                    error=f"HTTP {status}", elapsed_ms=_elapsed(started),
                )
                self._log(task, result, attempts=attempts)
                return result
            except httpx.TransportError as exc:
                last_error = str(exc)
                if attempt < max_retries:
                    self._backoff(attempt + 1, None)
                    continue
                result = FetchResult(
                    ok=False, status=status, doc=None, cached=False,
                    error=last_error, elapsed_ms=_elapsed(started),
                )
                self._log(task, result, attempts=attempts)
                return result

        result = FetchResult(
            ok=False, status=status, doc=None, cached=False,
            error=last_error, elapsed_ms=_elapsed(started),
        )
        self._log(task, result, attempts=attempts)
        return result

    def _backoff(self, attempt: int, response: httpx.Response | None) -> None:
        delay = None
        if response is not None:
            ra = response.headers.get("Retry-After")
            if ra and ra.isdigit():
                delay = float(ra)
        if delay is None:
            delay = (self.config.http.backoff_base ** attempt) + self.rng(0, 0.5)
        self.sleep(delay)

    def _log(self, task, result: FetchResult, *, attempts: int) -> None:
        if self.ctx is None:
            return
        error = result.error
        if attempts > 1:
            note = f"attempts={attempts}"
            error = f"{error}; {note}" if error else note
        self.ctx.log_fetch(
            source=getattr(task, "source", "http"),
            url=task.url,
            status=result.status,
            elapsed_ms=result.elapsed_ms,
            bytes=len(result.doc.body) if result.doc and result.doc.body else 0,
            cached=result.cached,
            domain=getattr(task, "domain", None),
            error=error,
        )


def _elapsed(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _attempts_note(attempts: int) -> str | None:
    if attempts > 1:
        return f"attempts={attempts}"
    return None
