"""DataDome bypass waterfall for sources behind DataDome bot protection.

5-tier waterfall:
  1. Cookie reuse (cached datadome cookie from DB, UA+proxy bound)
  2. curl_cffi TLS impersonation (chrome JA3/JA4 fingerprint)
  3. 2Captcha/CapSolver DataDomeSliderTask (returns datadome cookie)
  4. Headed fallback (visible browser, bounded wait)
  5. Hard stop (record datadome observation, invent nothing)
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from loguru import logger

from src.sources.techstack.datadome import (
    is_datadome_challenge,
    extract_datadome_params,
    extract_datadome_captcha_url,
    is_datadome_ip_banned,
)
from src.sources.techstack.datadome_solver import solve_datadome


@dataclass
class DataDomeOutcome:
    success: bool
    method: str | None          # "cookie_reuse" | "curl_cffi" | "stealth_browser" | "solver" | "headed" | None
    result_body: bytes | None
    cookies: list[dict]


class DataDomeBypass:
    def __init__(self, config, cookie_store, curl_fetcher, stealth_browser=None, shadow=None):
        self.config = config
        self.cookies = cookie_store
        self.curl = curl_fetcher
        self.stealth = stealth_browser
        # Optional SignalsShadow tier-1 (real Chrome TLS via src/antibot). When
        # present it runs after cookie reuse and before curl_cffi.
        self.shadow = shadow

    def attempt(self, *, domain: str, url: str, user_agent: str, proxy: str = "direct") -> DataDomeOutcome:
        dd = getattr(self.config, "datadome", None)
        if not dd or not dd.enabled or dd.bypass_strategy == "disabled":
            return DataDomeOutcome(False, None, None, [])

        # Tier 1: cached datadome cookie
        cached = self.cookies.get(domain, user_agent=user_agent, proxy=proxy)
        if cached:
            cached_cookies = json.loads(cached["cookies"])
            result = self._fetch_with_curl(url, cookies=cached_cookies, user_agent=user_agent, proxy=proxy)
            if result and result.status == 200 and not is_datadome_challenge(status=result.status, body=result.body):
                return DataDomeOutcome(True, "cookie_reuse", result.body, cached_cookies)
            self.cookies.clear(domain)

        # Tier 1.5: SignalsShadow (real Chrome TLS tier-1, antibot module)
        if self.shadow is not None:
            shadow_resp = None
            try:
                shadow_resp = self.shadow.fetch(
                    url,
                    cookies=cached_cookies if cached else None,
                    headers={"User-Agent": user_agent},
                )
            except Exception as e:
                logger.warning("signals_shadow fetch failed for {}: {}", url, e)
            if shadow_resp is not None:
                shadow_body = shadow_resp.body
                if not isinstance(shadow_body, bytes):
                    shadow_body = (shadow_body or "").encode("utf-8", "replace")
                if (
                    getattr(shadow_resp, "status", 0) == 200
                    and not is_datadome_challenge(status=shadow_resp.status, body=shadow_body)
                ):
                    shadow_cookies = self._shadow_cookies(shadow_resp, domain)
                    if shadow_cookies:
                        self._persist(domain, shadow_cookies, user_agent, proxy,
                                      method="signals_shadow")
                    return DataDomeOutcome(True, "signals_shadow", shadow_body, shadow_cookies)

        # Tier 2: curl_cffi TLS impersonation (no cookies, just right fingerprint)
        result = self._fetch_with_curl(url, cookies=None, user_agent=user_agent, proxy=proxy)
        if result and result.status == 200 and not is_datadome_challenge(status=result.status, body=result.body):
            # curl_cffi cleared it — extract the datadome cookie if present
            cookies = self._extract_cookies(result)
            if cookies:
                self._persist(domain, cookies, user_agent, proxy, method="curl_cffi")
            return DataDomeOutcome(True, "curl_cffi", result.body, cookies)

        # At this point we're challenged. Extract params.
        if not result or not is_datadome_challenge(status=result.status, body=result.body):
            # Not a DataDome challenge — something else is wrong
            return DataDomeOutcome(False, None, result.body if result else None, [])

        params = extract_datadome_params(result.body)
        if not params:
            return DataDomeOutcome(False, None, result.body, [])

        # Check if IP is banned (t=bv) — don't waste money on a solver
        if is_datadome_ip_banned(params):
            logger.warning("DataDome: IP banned (t=bv) for {} — change proxy", domain)
            return DataDomeOutcome(False, None, result.body, [])

        # Tier 2.5: stealth browser (Patchright) with behavioral warm-up
        if self.stealth and dd.bypass_strategy != "solver_first":
            try:
                warmup = f"https://www.{domain}/" if domain else None
                stealth_result = self.stealth.fetch(
                    url, source="marketplace_g2", domain=domain,
                    warmup_url=warmup, warmup_ms=4000,
                    cookies=cached_cookies if cached else None,
                    wait_ms=5000, scroll=True,
                )
                if stealth_result and stealth_result.ok and stealth_result.doc:
                    body = stealth_result.doc.body or b""
                    if not is_datadome_challenge(status=stealth_result.status, body=body):
                        # Extract datadome cookie from stealth browser cookies
                        dd_cookies = []
                        for c in (stealth_result.cloudflare_cookies or []):
                            if c.get("name") == "datadome":
                                dd_cookies.append(c)
                        if dd_cookies:
                            self._persist(domain, dd_cookies, user_agent, proxy, method="stealth_browser")
                        return DataDomeOutcome(True, "stealth_browser", body, dd_cookies)
            except Exception as e:
                logger.warning("Stealth browser tier failed: {}", e)

        # Tier 3: external solver (2Captcha/CapSolver)
        if dd.solver_provider and dd.solver_api_key and dd.bypass_strategy != "browser_only":
            captcha_url = extract_datadome_captcha_url(result.body)
            if captcha_url:
                # Use residential proxy if configured, otherwise the provided proxy
                solver_proxy = dd.residential_proxy or proxy
                cookie_str = solve_datadome(
                    captcha_url=captcha_url,
                    page_url=url,
                    provider=dd.solver_provider,
                    api_key=dd.solver_api_key,
                    user_agent=user_agent,
                    proxy=solver_proxy,
                )
                if cookie_str:
                    # Parse the cookie string into a list[dict]
                    solver_cookies = self._parse_cookie_string(cookie_str, domain)
                    # Re-fetch with the solved cookie
                    result2 = self._fetch_with_curl(url, cookies=solver_cookies, user_agent=user_agent, proxy=solver_proxy)
                    if result2 and result2.status == 200 and not is_datadome_challenge(status=result2.status, body=result2.body):
                        self._persist(domain, solver_cookies, user_agent, solver_proxy, method="solver")
                        return DataDomeOutcome(True, "solver", result2.body, solver_cookies)

        # Tier 4: headed fallback (stub — same as CloudflareBypass pattern)
        if dd.headed_fallback:
            # TODO: launch visible browser with curl_cffi cookies
            pass

        # Tier 5: hard stop
        return DataDomeOutcome(False, None, result.body, [])

    def _fetch_with_curl(self, url, *, cookies, user_agent, proxy):
        """Fetch using curl_cffi with TLS impersonation."""
        try:
            return self.curl.get(url, cookies=cookies, headers={"User-Agent": user_agent})
        except Exception as e:
            logger.warning("curl_cffi fetch failed for {}: {}", url, e)
            return None

    def _extract_cookies(self, result) -> list[dict]:
        """Extract datadome cookie from curl_cffi response."""
        cookies = []
        for name, value in result.cookies.items():
            if name == "datadome":
                cookies.append({"name": name, "value": value, "domain": ".g2.com"})
        return cookies

    def _shadow_cookies(self, shadow_resp, domain: str) -> list[dict]:
        """Extract the datadome cookie from a SignalsShadow response."""
        cookies = []
        for name, value in (getattr(shadow_resp, "cookies", None) or {}).items():
            if name == "datadome":
                cookies.append({"name": name, "value": value, "domain": f".{domain}"})
        return cookies

    def _parse_cookie_string(self, cookie_str: str, domain: str) -> list[dict]:
        """Parse a Set-Cookie style string into a list[dict]."""
        # cookie_str looks like: "datadome=abc123; Max-Age=31536000; Domain=.g2.com; Path=/; Secure; SameSite=Lax"
        parts = cookie_str.split(";")
        name_value = parts[0].strip()
        if "=" in name_value:
            name, value = name_value.split("=", 1)
            return [{"name": name.strip(), "value": value.strip(), "domain": domain}]
        return []

    def _persist(self, domain, cookies, user_agent, proxy, *, method):
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(hours=self.config.datadome.cookie_ttl_hours)).isoformat()
        self.cookies.put(domain, user_agent=user_agent, proxy=proxy,
                         cookies=cookies, expires_at=expires_at, solve_method=method)
