"""Undetected browser fetcher using Patchright (patched Chromium).

Patchright is a drop-in replacement for Playwright that patches Chromium at the
C++ source level to remove automation detection vectors (navigator.webdriver,
Runtime.enable CDP leak, --enable-automation flag, etc.). This allows it to
pass DataDome's device fingerprint check that stock Playwright fails.

The fetcher also supports a behavioral warm-up: navigate to a homepage first,
scroll, wait, then navigate to the target page. This produces the behavioral
signals (mouse movement, scroll events, dwell time) that DataDome's ML models
expect from a real user.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from loguru import logger

from src.core.http import FetchResult
from src.core.models import Document

if TYPE_CHECKING:
    from typing import Protocol

    from patchright.sync_api import Browser, BrowserContext, Page, Playwright
    from patchright.sync_api._generated import SetCookieParam

    from src.core.config import Config

    class _PutStoreLike(Protocol):
        """Put-shaped raw store accepted by this fetcher.

        Covers both ``RawStore`` and cli's no-op ``_RawShim`` (whose ``put``
        returns None, so it cannot subclass RawStore) — callers only ever
        hand the fetcher a store, this class never reads one back.
        """

        def put(self, *args: Any, **kwargs: Any) -> Any: ...


class PatchrightBrowserFetcher:
    """Undetected browser fetcher using Patchright.

    API mirrors BrowserFetcher but uses Patchright under the hood.
    Additional warmup_url/warmup_ms params for behavioral warm-up.
    """

    def __init__(self, config: Config, store: _PutStoreLike):
        self.config = config
        self.store = store
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    def start(self) -> None:
        """Launch Patchright browser."""
        from patchright.sync_api import sync_playwright
        self._playwright = sync_playwright().start()
        browser_cfg = self.config.browser
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
        ]
        self._browser = self._playwright.chromium.launch(
            headless=browser_cfg.headless,
            args=launch_args,
        )
        self._context = self._browser.new_context(
            viewport={"width": browser_cfg.viewport["width"], "height": browser_cfg.viewport["height"]},
            locale=browser_cfg.locale,
            timezone_id=browser_cfg.timezone,
            user_agent=browser_cfg.user_agent,
        )
        proxy_server = getattr(browser_cfg, "proxy_server", None)
        if proxy_server:
            self._context = self._browser.new_context(
                proxy={"server": proxy_server},
                viewport={"width": browser_cfg.viewport["width"], "height": browser_cfg.viewport["height"]},
                locale=browser_cfg.locale,
                timezone_id=browser_cfg.timezone,
                user_agent=browser_cfg.user_agent,
            )
        self._page = self._context.new_page()

    def fetch(self, url: str, *, source: str, domain: str | None = None,
              wait_selector: str | None = None, wait_ms: int | None = None,
              scroll: bool = False, capture_network: bool = False,
              capture_html: bool = False, click_show_more: bool = False,
              inject_turnstile_token: str | None = None,
              warmup_url: str | None = None, warmup_ms: int = 3000,
              cookies: list[SetCookieParam] | None = None) -> FetchResult:
        """Fetch a URL with optional behavioral warm-up.

        If warmup_url is set, navigate there first, scroll, wait warmup_ms,
        then navigate to url. This produces behavioral signals for DataDome.
        """
        if not self._page:
            self.start()

        page = self._page
        context = self._context
        # start() sets all four fields unconditionally (launch can raise), so
        # these guards are dead-branch defensive; they return the failure
        # contract rather than raise.
        if page is None:
            return FetchResult(ok=False, status=0, doc=None, cached=False,
                             error="Browser page unavailable", elapsed_ms=0)
        if context is None:
            return FetchResult(ok=False, status=0, doc=None, cached=False,
                             error="Browser context unavailable", elapsed_ms=0)

        # Inject cookies into context if provided
        if cookies:
            try:
                context.add_cookies(cookies)
            except Exception as e:
                logger.warning("Failed to inject cookies: {}", e)

        # Behavioral warm-up: visit homepage, scroll, wait
        if warmup_url:
            try:
                page.goto(warmup_url, wait_until="domcontentloaded")
                time.sleep(min(warmup_ms / 1000, 2))  # initial dwell
                # Simulate scroll
                page.mouse.wheel(0, 500)
                time.sleep(0.5)
                page.mouse.wheel(0, 300)
                time.sleep(max(warmup_ms / 1000 - 2, 1))  # remaining dwell
            except Exception as e:
                logger.warning("Warm-up navigation failed: {}", e)

        # Navigate to target
        try:
            page.goto(url, wait_until="domcontentloaded")
        except Exception as e:
            return FetchResult(ok=False, status=0, doc=None, cached=False,
                             error=f"Navigation failed: {e}", elapsed_ms=0)

        # Wait for JS to execute
        wait_seconds = (wait_ms or 5000) / 1000
        time.sleep(wait_seconds)

        # Optional scroll on target page
        if scroll:
            page.mouse.wheel(0, 800)
            time.sleep(1)

        # Optional Show More clicks
        if click_show_more:
            for sel in ["a:has-text('Show More')", "a:has-text('Read More')",
                       "button:has-text('Show More')", "[data-testid='show-more']"]:
                try:
                    elements = page.query_selector_all(sel)
                    for el in elements:
                        try:
                            el.click(timeout=2000)
                            page.wait_for_timeout(300)
                        except Exception:
                            pass
                except Exception:
                    pass

        # Extract page content
        html = page.content()
        body = html.encode("utf-8")

        # Extract cookies from context
        pw_cookies = context.cookies()
        cookie_list = [{"name": c["name"], "value": c["value"],
                        "domain": c.get("domain", ""), "path": c.get("path", "/"),
                        "expires": c.get("expires", -1)} for c in pw_cookies]

        # Store the document
        doc = Document(
            doc_id=f"{source}:{url}",
            source=source,
            url=url,
            body=body,
        )

        return FetchResult(
            ok=True, status=200, doc=doc, cached=False,
            error=None, elapsed_ms=int(wait_seconds * 1000),
            cloudflare_cookies=cookie_list,
        )

    def close(self) -> None:
        """Close browser and playwright."""
        try:
            if self._context:
                self._context.close()
        except Exception:
            pass
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright:
                self._playwright.stop()
        except Exception:
            pass
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
