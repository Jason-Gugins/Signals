"""Task 3 temporal stealth, Python side: conditional revalidation cache.

Chrome never re-downloads unchanged resources: it revalidates with
If-None-Match (ETag) / If-Modified-Since (Last-Modified) and serves the cached
body on 304. Scrapers never send validators — that's a temporal tell, same
class as "never resumes a TLS session". This module keeps the validator state
per URL and reconstructs the cached response from a 304.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # avoids a circular import: transport imports this module
    from .transport import AntibotResponse


@dataclass
class CacheEntry:
    """Stored validator + response payload for one URL."""

    status: int
    body: bytes
    headers: dict = field(default_factory=dict)
    cookies: dict = field(default_factory=dict)
    etag: str | None = None
    last_modified: str | None = None
    stored_at: float = 0.0


class RevalidationCache:
    """Per-URL conditional-revalidation cache (Chrome-like validator memory).

    - ``conditional_headers(url)`` → the If-None-Match / If-Modified-Since
      request headers to attach when we've seen the URL before.
    - ``on_response(url, response)`` → record the validator + body of a fresh
      200 response (honors ``Cache-Control: no-store``).
    - ``serve_304(url, response)`` → turn a 304 into the cached response
      (cached body, cached headers, ``from_cache=True``).
    """

    def __init__(self, max_entries: int = 512):
        self._entries: dict[str, CacheEntry] = {}
        self._max_entries = max_entries

    # -- lookup ----------------------------------------------------------

    def get(self, url: str) -> Optional[CacheEntry]:
        return self._entries.get(url)

    def has_validators(self, url: str) -> bool:
        e = self._entries.get(url)
        return bool(e and (e.etag or e.last_modified))

    # -- request side ----------------------------------------------------

    def conditional_headers(self, url: str) -> dict:
        """Validator request headers for a re-fetch of `url` (may be empty)."""
        e = self._entries.get(url)
        if e is None:
            return {}
        out: dict = {}
        if e.etag:
            out["if-none-match"] = e.etag
        if e.last_modified:
            out["if-modified-since"] = e.last_modified
        return out

    # -- response side ---------------------------------------------------

    def on_response(self, url: str, response: AntibotResponse) -> None:
        """Remember a fresh (200-class) response's validators + body."""
        if response.status < 200 or response.status >= 300:
            return
        cache_control = response.headers.get("cache-control", "").lower()
        if "no-store" in cache_control:
            self._entries.pop(url, None)
            return
        etag = response.headers.get("etag")
        last_modified = response.headers.get("last-modified")
        if not etag and not last_modified:
            # Nothing to revalidate with later — don't pretend we can 304.
            self._entries.pop(url, None)
            return
        if len(self._entries) >= self._max_entries:
            # FIFO eviction (simple, deterministic; Chrome uses LRU+heuristic).
            oldest = next(iter(self._entries))
            self._entries.pop(oldest, None)
        import time

        self._entries[url] = CacheEntry(
            status=response.status,
            body=response.body,
            headers=dict(response.headers),
            cookies=dict(response.cookies),
            etag=etag,
            last_modified=last_modified,
            stored_at=time.time(),
        )

    def serve_304(self, url: str, not_modified_response: "AntibotResponse") -> "AntibotResponse":
        """Materialize the cached response behind a 304.

        Chrome semantics: the user-visible response is the cached one (200),
        served from cache — the 304 is transport-internal. New validators sent
        on the 304 update the stored entry.
        """
        from .transport import AntibotResponse as _AR  # deferred: transport imports this module

        e = self._entries.get(url)
        if e is None:
            raise KeyError(f"no cached entry for {url}")
        # A 304 may carry updated validators — refresh them (Chrome does).
        for key in ("etag", "last-modified"):
            v = not_modified_response.headers.get(key)
            if v:
                if key == "etag":
                    e.etag = v
                else:
                    e.last_modified = v
                e.headers[key] = v
        return _AR(
            status=e.status,
            body=e.body,
            headers=dict(e.headers),
            cookies=dict(e.cookies),
            url=url,
            via=not_modified_response.via,
            reused_connection=not_modified_response.reused_connection,
            resumed_session=not_modified_response.resumed_session,
            from_cache=True,
        )

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


__all__ = ["CacheEntry", "RevalidationCache"]
