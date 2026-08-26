"""Cloudflare bypass waterfall for the techstack source."""
from __future__ import annotations
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from loguru import logger

from src.core.config import Config
from src.core.db import CfCookieStore
from src.core.http import FetchResult
from src.sources.techstack.cf_solver import solve_cloudflare
from src.sources.techstack.fingerprint import classify_cloudflare_challenge


@dataclass
class BypassOutcome:
    success: bool
    method: str | None          # "cookie_reuse" | "browser" | "solver" | "headed" | None
    challenge_type: str | None  # "js" | "managed"
    result: FetchResult | None
    cookies: list[dict]


class CloudflareBypass:
    def __init__(self, config: Config, cookie_store: CfCookieStore, http_fetcher, browser_fetcher):
        self.config = config
        self.cookies = cookie_store
        self.http = http_fetcher
        self.browser = browser_fetcher

    def attempt(self, *, domain: str, url: str, user_agent: str, proxy: str = "direct", source: str = "techstack", click_show_more: bool = False) -> BypassOutcome:
        cf = self.config.cloudflare
        if not cf.enabled or cf.bypass_strategy == "disabled":
            return BypassOutcome(False, None, "managed", None, [])

        # Tier 1: cached full cookie jar
        cached = self.cookies.get(domain, user_agent=user_agent, proxy=proxy)
        if cached:
            cached_cookies = json.loads(cached["cookies"])
            result = self._replay_with_cookies(url, cookies=cached_cookies, user_agent=user_agent)
            if result.ok and not self._is_challenge(result):
                return BypassOutcome(True, "cookie_reuse", None, result, cached_cookies)
            self.cookies.clear(domain)  # stale

        # Tier 2: browser solve (standard JS challenge)
        result: FetchResult | None = None
        if cf.bypass_strategy in ("browser_first", "browser_only", "solver_first"):
            result = self.browser.fetch(url, source=source, domain=domain, capture_html=True, click_show_more=click_show_more)
            if result.ok and result.cloudflare_cookies:
                expires_at = self._cookie_expiry(result.cloudflare_cookies)
                self._persist(domain, result.cloudflare_cookies, user_agent, proxy,
                              expires_at, method="browser")
                if not self._is_challenge(result):
                    return BypassOutcome(True, "browser", "js", result, result.cloudflare_cookies)

        # Tier 3: external solver (managed/Turnstile) — returns a Turnstile TOKEN, not a cookie.
        # Must re-enter the browser to inject the token → let CF set cf_clearance.
        if cf.solver_provider and cf.solver_api_key and cf.bypass_strategy != "browser_only":
            sitekey = self._extract_turnstile_sitekey(
                result.doc.body if result and result.doc else b"")
            token = solve_cloudflare(url=url, sitekey=sitekey,
                                     provider=cf.solver_provider, api_key=cf.solver_api_key,
                                     user_agent=user_agent)
            if token:
                # Re-enter browser: inject token into Turnstile callback, let CF set cookies
                result = self._solver_via_browser(url, domain, token, user_agent, source)
                if result and result.ok and result.cloudflare_cookies:
                    expires_at = self._cookie_expiry(result.cloudflare_cookies)
                    self._persist(domain, result.cloudflare_cookies, user_agent, proxy,
                                  expires_at, method="solver")
                    if not self._is_challenge(result):
                        return BypassOutcome(True, "solver", "managed", result, result.cloudflare_cookies)

        # Tier 4: headed manual fallback (bounded by headed_solve_timeout_ms)
        if cf.headed_fallback:
            result = self._headed_solve(url, domain, user_agent, proxy)
            if result and result.ok and result.cloudflare_cookies:
                expires_at = self._cookie_expiry(result.cloudflare_cookies)
                self._persist(domain, result.cloudflare_cookies, user_agent, proxy,
                              expires_at, method="headed")
                return BypassOutcome(True, "headed", "managed", result, result.cloudflare_cookies)

        # Tier 5: honest hard stop
        return BypassOutcome(False, None, "managed", None, [])

    # --- helpers ---

    def _is_challenge(self, result: FetchResult) -> bool:
        """True if the result body/status is still a Cloudflare challenge."""
        body = result.doc.body if result.doc else b""
        return classify_cloudflare_challenge(status=result.status, body=body) is not None

    def _replay_with_cookies(self, url: str, *, cookies: list[dict], user_agent: str) -> FetchResult:
        """Replay an HTTP GET with the full stored cookie jar."""
        return self.http.replay(url, cookies=cookies, user_agent=user_agent)

    def _cookie_expiry(self, cookies: list[dict]) -> str:
        """Compute expires_at from the real Playwright cookie `expires` (epoch),
        with cookie_ttl_hours as a ceiling. Returns tz-aware ISO."""
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        ceiling = now + timedelta(hours=self.config.cloudflare.cookie_ttl_hours)
        earliest = ceiling
        for c in cookies:
            exp = c.get("expires")
            if exp and isinstance(exp, (int, float)) and exp > 0:
                exp_dt = datetime.fromtimestamp(exp, tz=timezone.utc)
                if exp_dt < earliest:
                    earliest = exp_dt
        return earliest.isoformat()

    def _persist(self, domain, cookies, user_agent, proxy, expires_at, *, method):
        self.cookies.put(domain, user_agent=user_agent, proxy=proxy,
                         cookies=cookies, expires_at=expires_at, solve_method=method)

    def _extract_turnstile_sitekey(self, body: bytes) -> str | None:
        """Extract Turnstile sitekey from a FIRST-PARTY Turnstile widget only.
        For Cloudflare's own managed interstitial, the sitekey is NOT in the
        origin page DOM — return None so the solver uses AntiCloudflareTaskProxyless."""
        import re
        m = re.search(rb'data-sitekey="([^"]+)"', body)
        if m:
            return m.group(1).decode("utf-8", "replace")
        return None

    def _solver_via_browser(self, url, domain, token, user_agent, source="techstack") -> FetchResult | None:
        """Re-enter the browser to inject the Turnstile token into the callback,
        let Cloudflare set the real cf_clearance cookie, then extract the jar.
        Implementation: navigate to the challenge page, set the token via
        page.evaluate("document.querySelector('[name=cf-turnstile-response]').value = token"
        + dispatch the turnstile callback), wait for cf_clearance cookie, extract."""
        try:
            return self.browser.fetch(url, source=source, domain=domain,
                                      capture_html=True, inject_turnstile_token=token)
        except TypeError:
            # BrowserFetcher doesn't yet support inject_turnstile_token — skip
            logger.warning("BrowserFetcher lacks inject_turnstile_token; solver tier unavailable")
            return None

    def _headed_solve(self, url, domain, user_agent, proxy) -> FetchResult | None:
        """Launch a visible browser, wait up to headed_solve_timeout_ms for a
        manual solve, then extract cookies. Returns None on timeout."""
        # Implementation: temporarily set config.browser.headless=False, call
        # browser.fetch(capture_html=True), restore headless. Bounded by the
        # solve_timeout in _challenge_cleared via headed_solve_timeout_ms.
        # For initial implementation this is a stub that returns None unless
        # the browser is already headed.
        try:
            self.config.browser.headless = False
            result = self.browser.fetch(url, source="techstack", domain=domain, capture_html=True)
            self.config.browser.headless = True
            return result
        except Exception:
            self.config.browser.headless = True
            return None
