"""Playwright browser-tier fetcher. Disabled unless config.browser.enabled."""

from __future__ import annotations

import json
import random
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from loguru import logger

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

    Primary signal: ``page.title()`` no longer a challenge title — this fires
    when Cloudflare's challenge JS completes and reloads the page.
    Secondary signal: ``cf_clearance`` cookie presence (set during JS execution,
    but the page may not have reloaded yet — don't return True on cookie alone).
    """
    import time
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        # Primary: title no longer a challenge title (page has reloaded with real content)
        try:
            title = (page.title() or "").lower()
            if not any(t in title for t in _CHALLENGE_TITLES):
                return True
        except Exception:
            pass
        # Secondary: cf_clearance cookie present (but page may not have reloaded yet)
        # — used only to confirm progress, not to declare success.
        page.wait_for_timeout(poll_ms)
    return False


def _wait_for_real_content(page, *, timeout_ms: int = 10000, poll_ms: int = 500) -> None:
    """After cf_clearance is set, Cloudflare reloads the page. Wait for the
    title to stop being a challenge title and the body to gain real content."""
    import time
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        try:
            title = (page.title() or "").lower()
            if not any(t in title for t in _CHALLENGE_TITLES):
                # Title cleared — give the body a moment to render, then done.
                page.wait_for_timeout(500)
                return
        except Exception:
            pass
        page.wait_for_timeout(poll_ms)


def inject_turnstile_token(browser, url, token, *, timeout_ms: int = 20000, poll_ms: int = 500) -> bool:
    """Inject a solved Turnstile token as a cf_clearance cookie and verify
    challenge clearance.

    Steps: navigate the existing browser context to ``url``, set
    ``cf_clearance`` on the context via ``add_cookies`` (domain derived from
    the URL host), reload, then poll challenge-cleared (title change +
    real-content check) up to ``timeout_ms``. Returns True if cleared,
    False otherwise. Never raises — any error yields False so the caller
    can fall through to the next bypass tier.

    ``browser`` is a BrowserFetcher (or duck-typed object exposing
    ``_page`` and ``_context``).
    """
    try:
        context = browser._context
        page = browser._page
        host = (urlsplit(url).hostname or "").casefold()
        if not host or not token:
            return False
        page.goto(url, wait_until="domcontentloaded")
        context.add_cookies([
            {"name": "cf_clearance", "value": token, "domain": host, "path": "/"},
        ])
        page.reload(wait_until="domcontentloaded")
        if not _challenge_cleared(page, context, domain=host,
                                  timeout_ms=timeout_ms, poll_ms=poll_ms):
            return False
        try:
            _wait_for_real_content(page, timeout_ms=5000, poll_ms=poll_ms)
        except Exception:
            pass
        return True
    except Exception as e:
        logger.warning("inject_turnstile_token failed for {}: {}", url, e)
        return False


class BrowserFetcher:
    def __init__(
        self,
        config: Config,
        store: RawStore,
        ctx: RunContext | None = None,
        cookie_jar=None,
    ):
        self.config = config
        self.store = store
        self.ctx = ctx
        self.cookie_jar = cookie_jar
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    @staticmethod
    def _build_context_args(config: Config, *, skip_storage_state: bool = False) -> dict:
        args: dict = {
            "viewport": config.browser.viewport,
            "locale": config.browser.locale,
            "timezone_id": config.browser.timezone,
            "user_agent": config.browser.user_agent,
        }
        if config.browser.proxy_server:
            args["proxy"] = {"server": config.browser.proxy_server}
        if not skip_storage_state:
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

    def _new_solve_context(self):
        """Create a fresh browser context WITHOUT storage_state for challenge solves.

        Returns (context, page). The caller is responsible for closing the context
        when done. The stealth init script is applied automatically.
        """
        ctx = self._browser.new_context(
            **self._build_context_args(self.config, skip_storage_state=True)
        )
        # Cookie-jar hook (Task 23): seed the fresh context with jar cookies
        # for this URL so challenge solves start from accumulated state.
        if self.cookie_jar is not None and getattr(self, "_jar_seed_url", None):
            try:
                seed = self.cookie_jar.cookies_for(self._jar_seed_url)
                if seed:
                    ctx.add_cookies(
                        [
                            {
                                "name": c["name"],
                                "value": c["value"],
                                "domain": c["domain"],
                                "path": "/",
                            }
                            for c in seed
                        ]
                    )
            except Exception:
                pass
        ctx.add_init_script(STEALTH_INIT_SCRIPT)
        page = ctx.new_page()
        return ctx, page

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
        click_show_more: bool = False,
        inject_turnstile_token: str | None = None,
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
            if click_show_more:
                try:
                    selectors = [
                        "a:has-text('Show More')",
                        "a:has-text('Read More')",
                        "button:has-text('Show More')",
                        "[data-testid='show-more']",
                    ]
                    for sel in selectors:
                        try:
                            elements = self._page.query_selector_all(sel)
                            for el in elements:
                                try:
                                    el.click(timeout=2000)
                                    self._page.wait_for_timeout(300)
                                except Exception:
                                    pass
                        except Exception:
                            pass
                except Exception:
                    pass
            if inject_turnstile_token:
                try:
                    self._page.evaluate(f"""
                        () => {{
                            const input = document.querySelector('[name="cf-turnstile-response"]');
                            if (input) input.value = "{inject_turnstile_token}";
                            if (window.turnstile) {{
                                const widget = document.querySelector('[data-sitekey]');
                                if (widget) window.turnstile.execute(widget.getAttribute('data-turnstile-id'), {{token: "{inject_turnstile_token}"}});
                            }}
                        }}
                    """)
                    self._page.wait_for_timeout(3000)
                except Exception:
                    pass
            if capture_html:
                # Use a fresh context (no stale storage_state) when a real
                # browser is running. Fall back to stubs in test mode.
                solve_ctx = None
                solve_page = self._page
                solve_context = self._context
                if self._browser is not None:
                    # Cookie-jar hook (Task 23): remember the URL so
                    # _new_solve_context can seed the fresh context from the jar.
                    self._jar_seed_url = url
                    solve_ctx, solve_page = self._new_solve_context()
                    self._jar_seed_url = None
                    solve_context = solve_ctx
                    solve_page.goto(url, wait_until="domcontentloaded")
                try:
                    # Challenge-aware: poll for title change (page reloads
                    # after challenge JS completes), then capture content + cookies.
                    timeout_ms = self.config.cloudflare.solve_timeout_ms
                    cleared = _challenge_cleared(
                        solve_page, solve_context,
                        domain=domain or "", timeout_ms=timeout_ms, poll_ms=500,
                    )
                    if cleared:
                        _wait_for_real_content(solve_page, timeout_ms=5000, poll_ms=500)
                        try:
                            title = (solve_page.title() or "").lower()
                            if any(t in title for t in _CHALLENGE_TITLES):
                                solve_page.goto(url, wait_until="domcontentloaded")
                                _wait_for_real_content(solve_page, timeout_ms=10000, poll_ms=500)
                        except Exception:
                            pass
                    body = solve_page.content().encode("utf-8")
                    ctype = "text/html"
                    # Extract the full cookie jar from the solve context.
                    root = (domain or "").casefold().lstrip(".")
                    try:
                        for c in solve_context.cookies():
                            cd = (c.get("domain", "") or "").casefold().lstrip(".")
                            if cd == root or cd.endswith("." + root) or root in (c.get("domain", "") or ""):
                                cf_cookies.append(c)
                                # Cookie-jar hook (Task 23): accumulate the
                                # solve context's cookies into the persistent jar.
                                if self.cookie_jar is not None:
                                    try:
                                        self.cookie_jar.update(
                                            c.get("name", ""),
                                            c.get("value", ""),
                                            c.get("domain", "") or (domain or ""),
                                            expires=c.get("expires"),
                                        )
                                    except Exception:
                                        pass
                    except Exception:
                        pass
                finally:
                    if solve_ctx is not None:
                        try:
                            solve_page.close()
                        except Exception:
                            pass
                        try:
                            solve_ctx.close()
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
