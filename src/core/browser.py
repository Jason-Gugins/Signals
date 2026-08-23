"""Playwright browser-tier fetcher. Disabled unless config.browser.enabled."""

from __future__ import annotations

import json
import random
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from src.core.config import Config
from src.core.http import FetchResult
from src.core.rawstore import RawStore
from src.core.runlog import RunContext
from src.core.stealth import STEALTH_INIT_SCRIPT


class BrowserDisabled(RuntimeError):
    """Raised when a source tries to use the browser tier while it is off."""


def _select_har_requests(reqs: list[dict], *, domain: str | None, limit: int = 80) -> list[dict]:
    third, first = [], []
    seen: set[str] = set()
    root = (domain or "").casefold().lstrip(".")
    for r in reqs:
        h = r.get("host") or ""
        if not h or h in seen:
            continue
        seen.add(h)
        if root and (h == root or h.endswith("." + root)):
            first.append(r)
        else:
            third.append(r)
    return (third + first)[:limit]


_CHALLENGE_TITLES = ("just a moment", "checking your browser")


def _challenge_cleared(page, context, *, domain: str, timeout_ms: int, poll_ms: int = 500) -> bool:
    """Poll for Cloudflare challenge clearance.

    Primary signal: ``cf_clearance`` cookie presence in ``context.cookies()``
    (authoritative — set exactly when the challenge clears).
    Secondary signal: ``page.title()`` no longer a challenge title.
    """
    import time
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        # Primary: cf_clearance cookie presence (authoritative — set exactly when challenge clears)
        try:
            for c in context.cookies():
                if c.get("name") == "cf_clearance" and domain in (c.get("domain", "") or ""):
                    return True
        except Exception:
            pass
        # Secondary: title no longer a challenge title
        try:
            title = (page.title() or "").lower()
            if not any(t in title for t in _CHALLENGE_TITLES):
                return True
        except Exception:
            pass
        page.wait_for_timeout(poll_ms)
    return False


class BrowserFetcher:
    def __init__(self, config: Config, store: RawStore, ctx: RunContext | None = None):
        self.config = config
        self.store = store
        self.ctx = ctx
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    @staticmethod
    def _build_context_args(config: Config) -> dict:
        args: dict = {
            "viewport": config.browser.viewport,
            "locale": config.browser.locale,
            "timezone_id": config.browser.timezone,
            "user_agent": config.browser.user_agent,
        }
        if config.browser.proxy_server:
            args["proxy"] = {"server": config.browser.proxy_server}
        session_path = Path(config.browser.session_dir) / "session.json"
        if session_path.exists():
            args["storage_state"] = str(session_path)
        return args

    def start(self) -> "BrowserFetcher":
        if not self.config.browser.enabled:
            raise BrowserDisabled("browser tier is disabled (config.browser.enabled=false)")
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=self.config.browser.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--disable-infobars",
            ],
        )
        self._context = self._browser.new_context(**self._build_context_args(self.config))
        self._context.add_init_script(STEALTH_INIT_SCRIPT)
        self._page = self._context.new_page()
        return self

    def fetch(
        self,
        url: str,
        *,
        source: str,
        domain: str | None = None,
        wait_selector: str | None = None,
        wait_ms: int | None = None,
        scroll: bool = False,
        capture_network: bool = False,
        capture_html: bool = False,
    ) -> FetchResult:
        if not self.config.browser.enabled:
            raise BrowserDisabled("browser tier is disabled (config.browser.enabled=false)")
        if self._page is None:
            self.start()
        started = time.monotonic()
        reqs: list[dict] = []

        def on_req(r):
            raw_url = getattr(r, "url", "") or ""
            parts = urlsplit(raw_url)
            if parts.scheme in {"data", "blob"}:
                return
            clean = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
            reqs.append(
                {
                    "url": clean,
                    "host": (parts.hostname or "").casefold(),
                    "resource_type": getattr(r, "resource_type", "") or "",
                }
            )

        if capture_network:
            if wait_ms is None:
                wait_ms = 2500
            self._page.on("request", on_req)
        cf_cookies: list[dict] = []
        try:
            self._page.goto(url, wait_until="domcontentloaded")
            if wait_selector:
                self._page.wait_for_selector(wait_selector)
            if wait_ms:
                self._page.wait_for_timeout(wait_ms)
            if scroll:
                self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            if capture_html:
                # Challenge-aware: poll for cf_clearance cookie presence (primary)
                # + challenge-title absence (secondary), then capture content + cookies.
                timeout_ms = self.config.cloudflare.solve_timeout_ms
                _challenge_cleared(
                    self._page, self._context,
                    domain=domain or "", timeout_ms=timeout_ms, poll_ms=500,
                )
                body = self._page.content().encode("utf-8")
                ctype = "text/html"
                # Extract the full cookie jar (cf_clearance + __cf_bm + cf_chl_*) for the domain.
                root = (domain or "").casefold().lstrip(".")
                try:
                    for c in self._context.cookies():
                        cd = (c.get("domain", "") or "").casefold().lstrip(".")
                        if cd == root or cd.endswith("." + root) or root in (c.get("domain", "") or ""):
                            cf_cookies.append(c)
                except Exception:
                    pass
            elif capture_network:
                payload = {"page_url": url, "requests": _select_har_requests(reqs[:200], domain=domain, limit=80)}
                body = json.dumps(payload).encode("utf-8")
                ctype = "application/json"
            else:
                body = self._page.content().encode("utf-8")
                ctype = "text/html"
        finally:
            if capture_network:
                self._page.remove_listener("request", on_req)
        doc = self.store.put(
            source=source,
            url=url,
            body=body,
            content_type=ctype,
            status=200,
            domain=domain,
        )
        result = FetchResult(
            ok=True,
            status=200,
            doc=doc,
            cached=False,
            error=None,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            cloudflare_cookies=cf_cookies,
        )
        if self.ctx:
            self.ctx.log_fetch(
                source=source,
                url=url,
                status=200,
                elapsed_ms=result.elapsed_ms,
                bytes=len(body),
                domain=domain,
            )
        return result

    def human_pause(self) -> None:
        lo = self.config.browser.min_delay
        hi = self.config.browser.max_delay
        time.sleep(random.uniform(lo, hi))

    def save_state(self) -> None:
        if self._context is None:
            return
        path = Path(self.config.browser.session_dir) / "session.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._context.storage_state(path=str(path))

    def close(self) -> None:
        try:
            self.save_state()
        except Exception:
            pass
        if self._page:
            self._page.close()
            self._page = None
        if self._context:
            self._context.close()
            self._context = None
        if self._browser:
            self._browser.close()
            self._browser = None
        if self._playwright:
            self._playwright.stop()
            self._playwright = None

    def __enter__(self) -> "BrowserFetcher":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()
