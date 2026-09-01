"""Core-side persistent cookie jar (Task 23).

Reuses the antibot jar's implementation by import — same semantics:
caller-wins merge (an explicitly passed cookie overrides a stored one),
JSON persistence, host/domain-suffix matching, expiry handling.

The only core-specific addition is per-scope files: each scope (``http``,
``curl``, ``browser``) persists to ``data/cookies/<scope>.json`` so the
three transports don't clobber each other's cookie state.

Import coupling is acceptable here: ``src/core`` already depends on
antibot-tier modules indirectly via the pipeline, and the antibot cookies
module is pure-stdlib with no antibot-package dependencies, so importing
it from core adds no new package-level cycle.
"""

from __future__ import annotations

from pathlib import Path

from src.antibot.python.cookies import CookieEntry, PersistentCookieJar as _AntibotJar

__all__ = ["CookieEntry", "PersistentCookieJar"]

DEFAULT_COOKIE_ROOT = "data/cookies"


class PersistentCookieJar(_AntibotJar):
    """PersistentCookieJar scoped to a per-transport file under data/cookies/."""

    def __init__(
        self,
        path: str | Path | None = None,
        scope: str = "http",
        root: str | Path = DEFAULT_COOKIE_ROOT,
        clock=None,
    ):
        import time

        if path is None:
            path = Path(root) / f"{scope}.json"
        if clock is None:
            clock = time.time
        super().__init__(path=path, clock=clock)
        self.scope = scope
