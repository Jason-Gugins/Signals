"""Solve-and-bounce orchestrator: ghost browser solves, TLS tier fetches.

The "ghost" is the Patchright stealth browser (src/core/patchright_browser.py).
It exists only to solve challenges (DataDome, Cloudflare) and harvest
clearance cookies from its browser context. Those cookies are handed to the
fast TLS tier (SignalsTransport) which fetches the actual content. The
browser never fetches content itself.

BounceResult shapes:
- via='tier1_direct'            : tier-1 response was clean; response set, cookies [].
- via='tier1_with_ghost_cookies': ghost solved, cookies injected, refetch returned.
- via='solve_failed'            : ghost produced no clearance cookies or raised;
                                  response is None (no refetch happened).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Protocol

from src.antibot.python.transport import AntibotResponse

# Clearance-cookie names worth keeping. Everything else (analytics, ads,
# A/B junk like _ga/_gid/_fbp) is dropped.
_CLEARANCE_NAMES = {"cf_clearance", "datadome", "__cf_bm", "_abck"}
_CLEARANCE_PREFIXES = ("_px",)


def extract_clearance_cookies(cookies: list[dict]) -> list[dict]:
    """Keep only clearance cookies from a browser-context cookie list."""
    kept: list[dict] = []
    for c in cookies or []:
        name = c.get("name", "")
        if name in _CLEARANCE_NAMES or name.startswith(_CLEARANCE_PREFIXES):
            kept.append(c)
    return kept


@dataclass
class GhostResult:
    """What a ghost produced after solving a challenge."""

    clearance_cookies: list[dict] = field(default_factory=list)
    rendered_html: bytes | None = None


class Ghost(Protocol):
    """A challenge solver. Implementations must NOT fetch content."""

    def solve(self, url: str) -> GhostResult: ...


class PatchrightGhost:
    """Ghost backed by PatchrightBrowserFetcher, forced headed.

    Headed mode is what clears DataDome. The patchright import happens
    inside the underlying fetcher's start(), so it stays deferred until the
    browser is actually needed. Lifecycle is simple: the browser starts
    lazily on first solve() and lives until close().
    """

    def __init__(self, fetcher):
        self._fetcher = fetcher

    def solve(self, url: str) -> GhostResult:
        # Force headed: DataDome detects headless Chromium reliably.
        browser_cfg = self._fetcher.config.browser
        if getattr(browser_cfg, "headless", False):
            try:
                self._fetcher.config.browser.headless = False
            except Exception:
                copied = copy.copy(self._fetcher.config)
                copied.browser = copy.copy(browser_cfg)
                copied.browser.headless = False
                self._fetcher.config = copied
        result = self._fetcher.fetch(url, source="ghost", domain=None)
        cookies = extract_clearance_cookies(getattr(result, "cloudflare_cookies", []) or [])
        html = result.doc.body if result.ok and result.doc is not None else None
        return GhostResult(clearance_cookies=cookies, rendered_html=html)

    def close(self):
        self._fetcher.close()


@dataclass
class BounceResult:
    via: str
    response: AntibotResponse | None
    clearance_cookies: list[dict] = field(default_factory=list)


def _default_challenge_check(response: AntibotResponse) -> bool:
    """DataDome or Cloudflare challenge detection on a tier-1 response."""
    from src.sources.techstack.datadome import is_datadome_challenge
    from src.sources.techstack.fingerprint import classify_cloudflare_challenge

    if is_datadome_challenge(status=response.status, body=response.body):
        return True
    return classify_cloudflare_challenge(status=response.status, body=response.body) is not None


def solve_and_bounce(
    transport,
    ghost,
    url: str,
    *,
    challenge_check=None,
) -> BounceResult:
    """Fetch via the fast TLS tier; if challenged, let the ghost solve and retry.

    The ghost only solves and harvests cookies; the transport (re)fetches all
    content with the harvested clearance cookies injected.
    """
    response = transport.fetch(url)
    check = challenge_check or _default_challenge_check
    if not check(response):
        return BounceResult(via="tier1_direct", response=response, clearance_cookies=[])

    try:
        solved = ghost.solve(url)
    except Exception:
        return BounceResult(via="solve_failed", response=None, clearance_cookies=[])

    if not solved.clearance_cookies:
        return BounceResult(via="solve_failed", response=None, clearance_cookies=[])

    refetched = transport.fetch(url, cookies=solved.clearance_cookies)
    return BounceResult(
        via="tier1_with_ghost_cookies",
        response=refetched,
        clearance_cookies=solved.clearance_cookies,
    )


__all__ = [
    "extract_clearance_cookies",
    "Ghost",
    "GhostResult",
    "PatchrightGhost",
    "BounceResult",
    "solve_and_bounce",
]