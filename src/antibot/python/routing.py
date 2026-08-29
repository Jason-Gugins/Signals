"""Self-improving routing state machine: Warm/Cold/SkipToSolve/RecheckCold.

Part of the anti-bot module (src/antibot). NOTE: this module is *deliberately*
NOT pure with respect to time — it defaults to ``time.time()``. The source
purity guard (tests/test_source_purity.py) scans ``src/sources/`` only;
``src/antibot/`` is exempt from that scan. Time is nonetheless injectable via
the ``clock`` constructor argument so tests can time-travel without sleeps.

Lifetime learning (DonSeTch's convergence rule): each expire() cycle observes
how long clearance cookies actually lasted; the per-domain estimate is the
``min`` across cycles, so the estimate only ever tightens.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

COLD = "Cold"
WARM = "Warm"
SKIP_TO_SOLVE = "SkipToSolve"
RECHECK_COLD = "RecheckCold"

#: How long to keep skipping the doomed tier-1 attempt before periodically
#: retrying it cold (the challenge may have been lifted).
DEFAULT_RECHECK_SECONDS = 24 * 3600

CLEARANCE_COOKIES = {"cf_clearance", "datadome", "_abck", "_pxhd", "_px3"}


class RouteState:
    """Per-domain routing state with cookie-lifetime learning.

    State per domain: ``{"state": str, "solved_at": epoch_seconds,
    "lifetime": seconds | None}``.
    """

    def __init__(
        self,
        path: str = "data/antibot/routing.json",
        *,
        clock: Callable[[], float] = time.time,
        recheck_seconds: int = DEFAULT_RECHECK_SECONDS,
    ) -> None:
        self.path = Path(path)
        self.clock = clock
        self.recheck_seconds = recheck_seconds
        self._domains: dict[str, dict] = {}
        self._load()

    # ------------------------------------------------------------------ API

    def decide(self, domain: str) -> str:
        """Route decision for *domain*."""
        entry = self._domains.get(domain)
        now = self.clock()
        if entry is None:
            return COLD
        state = entry.get("state", COLD)
        if state == WARM:
            lifetime = entry.get("lifetime")
            solved_at = entry.get("solved_at", now)
            if lifetime is not None and now - solved_at > lifetime:
                # Cookies are known to have aged out: mark expired so the
                # next cycle skips the doomed tier-1 attempt.
                entry["state"] = SKIP_TO_SOLVE
                entry["expired_at"] = now
                self._save()
                return SKIP_TO_SOLVE
            return WARM
        if state == SKIP_TO_SOLVE:
            expired_at = entry.get("expired_at", entry.get("solved_at", now))
            if now - expired_at >= self.recheck_seconds:
                return RECHECK_COLD
            return SKIP_TO_SOLVE
        return COLD

    def record_solve(self, domain: str, *, cookies: list[str]) -> None:
        """Record that a browser solve succeeded for *domain* at now."""
        self._domains[domain] = {
            "state": WARM,
            "solved_at": self.clock(),
            "lifetime": self._domains.get(domain, {}).get("lifetime"),
        }
        self._save()

    def expire(self, domain: str, *, after_seconds: int | None = None) -> None:
        """Mark *domain*'s cookies expired, learning the observed lifetime.

        ``after_seconds`` is how long the cookies actually lasted since
        record_solve (defaults to wall time since solve). The learned
        lifetime is ``min(previous, observed)`` — convergence.
        """
        entry = self._domains.get(domain)
        if entry is None:
            return
        now = self.clock()
        observed = (
            after_seconds
            if after_seconds is not None
            else now - entry.get("solved_at", now)
        )
        previous = entry.get("lifetime")
        entry["lifetime"] = (
            observed if previous is None else min(previous, observed)
        )
        entry["state"] = SKIP_TO_SOLVE
        entry["expired_at"] = now
        self._save()

    def observed_lifetime(self, domain: str) -> int | None:
        """Learned cookie lifetime in seconds, or None if never observed."""
        entry = self._domains.get(domain)
        if entry is None:
            return None
        return entry.get("lifetime")

    def force_state(
        self, domain: str, state: str, *, age_seconds: int = 0
    ) -> None:
        """Force a state (tests / time travel). Timestamps are aged back."""
        now = self.clock()
        self._domains[domain] = {
            "state": state,
            "solved_at": now - age_seconds,
            "expired_at": now - age_seconds,
            "lifetime": self._domains.get(domain, {}).get("lifetime"),
        }
        self._save()

    # --------------------------------------------------------- persistence

    def _load(self) -> None:
        try:
            self._domains = json.loads(self.path.read_text())
            if not isinstance(self._domains, dict):
                self._domains = {}
        except (OSError, ValueError):
            self._domains = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._domains, indent=2))