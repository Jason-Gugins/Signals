"""Empty-slug bookkeeping for marketplace sources ('empty since' tracking).

Mirrors :class:`src.antibot.python.routing.RouteState`: a small JSON state
file, an injectable ``clock`` (defaults to ``time.time`` so tests can
time-travel), tolerant of missing/corrupt state.

A slug that renders zero reviews ("empty") is not a block — it's usually a
new or quiet product — but polling it every cadence wastes anti-bot budget.
After ``threshold_cycles`` consecutive empty harvests the slug goes into
backoff and the runner skips planning it ("skipping (empty since X)").
Any cycle that returns reviews resets the counter.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

DEFAULT_EMPTY_LOG_PATH = "data/antibot/empty_slugs.json"
DEFAULT_THRESHOLD_CYCLES = 3


class EmptyLog:
    """Per-(source, slug) consecutive-empty-cycle tracker backed by JSON."""

    def __init__(
        self,
        path: str | Path = DEFAULT_EMPTY_LOG_PATH,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path)
        self.clock = clock
        self._entries: dict[str, dict] = {}
        self._load()

    # ------------------------------------------------------------------ API

    @staticmethod
    def _key(slug: str, source: str) -> str:
        return f"{source}:{slug}"

    def record_empty(self, slug: str, source: str) -> None:
        """Record an empty harvest for *slug*: bump the consecutive counter."""
        # Re-read the state file first: callers hold exclusive_lock(path)
        # around this call, so the refresh makes the locked section a true
        # read-modify-write even when another process wrote since our __init__.
        self._load()
        key = self._key(slug, source)
        now = self.clock()
        entry = self._entries.get(key) or {}
        # UTC, not local time: dates must be deterministic regardless of the
        # machine's timezone (CI runners are UTC; dev boxes often are not).
        first_empty = entry.get("first_empty") or datetime.fromtimestamp(
            now, tz=timezone.utc
        ).date().isoformat()
        self._entries[key] = {
            "first_empty": first_empty,
            "cycles": int(entry.get("cycles", 0)) + 1,
            "last_empty": datetime.fromtimestamp(now, tz=timezone.utc).date().isoformat(),
        }
        self._save()

    def record_reviews(self, slug: str, source: str) -> None:
        """A cycle with reviews: reset (remove) the slug's empty state."""
        # Refresh from disk under the caller's exclusive_lock(path): a reset
        # must not clobber entries another process recorded since __init__.
        self._load()
        key = self._key(slug, source)
        if key in self._entries:
            del self._entries[key]
            self._save()

    def is_in_backoff(
        self, slug: str, source: str, *, threshold_cycles: int = DEFAULT_THRESHOLD_CYCLES
    ) -> bool:
        """True after ``threshold_cycles`` consecutive empty harvests."""
        entry = self._entries.get(self._key(slug, source))
        return bool(entry) and int(entry.get("cycles", 0)) >= threshold_cycles

    def entry(self, slug: str, source: str) -> dict:
        """Raw state entry for the (source, slug) pair; {} when none."""
        return dict(self._entries.get(self._key(slug, source)) or {})

    # ------------------------------------------------------------------ I/O

    def _load(self) -> None:
        try:
            if not self.path.exists():
                return
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._entries = data
        except (OSError, ValueError):
            self._entries = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self._entries, indent=2, sort_keys=True), encoding="utf-8"
        )
        os.replace(tmp, self.path)
