"""Durable per-source scheduling for the watch loop.

Decides which sources are due for collection based on:
- per-source cadence (from config/sources.yaml),
- last-success state persisted in the ``source_cursors`` table (survives
  restarts — a source whose cadence elapsed while the process was down is
  due immediately: missed-run catchup is implicit),
- ±5% jitter on the scheduled cadence,
- exponential backoff on consecutive failures (cadence × 2^failures, capped
  at 8x, reset on success),
- a single-flight lockfile (data/state/collect.lock) so two watch processes
  never collect concurrently; a stale lock (dead pid or older than 24h) is
  broken automatically.

Note: for production-grade reliability, driving ``collect`` per-source from
OS cron remains the recommended setup — this scheduler makes the in-process
watch loop durable, it does not replace process supervision.
"""

from __future__ import annotations

import os
import random
import time
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger

DEFAULT_CADENCE_HOURS = 24.0
JITTER_FRACTION = 0.05          # ±5% of cadence
BACKOFF_BASE = 2
MAX_BACKOFF_EXPONENT = 3        # 2**3 == 8x cap
LOCK_MAX_AGE_S = 24 * 3600      # a lock older than 24h is stale


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


class Scheduler:
    """Per-source due-decision + durable outcome bookkeeping.

    State lives in ``source_cursors`` under the reserved key ``'global'``
    per source (fanout adapters already use that key, non-fanout adapters
    keep their per-domain rows untouched).
    """

    def __init__(self, db, cadences: dict[str, float], *, rng=None,
                 default_cadence_hours: float = DEFAULT_CADENCE_HOURS):
        self.db = db
        self.cadences = dict(cadences or {})
        self.default_cadence_hours = float(default_cadence_hours)
        self._rng = rng or random.Random()

    # ── config ──────────────────────────────────────────────────────────

    def cadence(self, source: str) -> float:
        return float(self.cadences.get(source, self.default_cadence_hours))

    def jittered(self, hours: float) -> float:
        """Apply ±5% multiplicative jitter (deterministic with injected rng)."""
        return hours * (1.0 + (self._rng.random() * 2.0 - 1.0) * JITTER_FRACTION)

    # ── state ───────────────────────────────────────────────────────────

    def _cursor(self, source: str) -> dict | None:
        return self.db.one(
            "SELECT * FROM source_cursors WHERE source=? AND key=?", (source, "global")
        )

    def effective_cadence(self, source: str) -> float:
        """Cadence with failure backoff applied (capped at 8x)."""
        row = self._cursor(source) or {}
        n = int(row.get("fail_count") or 0)
        return self.cadence(source) * (BACKOFF_BASE ** min(n, MAX_BACKOFF_EXPONENT))

    # ── decisions ───────────────────────────────────────────────────────

    def decide_due(self, adapters, *, now: datetime) -> list:
        """Return the adapters whose collection is due at ``now``.

        A source is due when it has never run, its next_due_at is blank,
        or the (jittered, backoff-aware) cadence has elapsed — which makes
        missed-run catchup after downtime implicit.
        """
        iso = _iso(now)
        due = []
        for adapter in adapters:
            row = self._cursor(adapter.key)
            if row is None or not row.get("next_due_at") or row["next_due_at"] <= iso:
                due.append(adapter)
        return due

    # ── outcome recording (makes jitter + backoff durable) ──────────────

    def record_success(self, source: str, *, now: datetime) -> None:
        hours = self.jittered(self.cadence(source))
        self.db.upsert(
            "source_cursors",
            {
                "source": source,
                "key": "global",
                "cursor": None,
                "etag": None,
                "last_modified": None,
                "last_run_at": _iso(now),
                "next_due_at": _iso(now + timedelta(hours=hours)),
                "fail_count": 0,
                "last_error": None,
            },
            pk=("source", "key"),
            overwrite={"last_run_at", "next_due_at", "fail_count", "last_error"},
        )

    def record_failure(self, source: str, *, now: datetime, error: str = "") -> None:
        row = self._cursor(source) or {}
        n = int(row.get("fail_count") or 0) + 1
        hours = self.jittered(
            self.cadence(source) * (BACKOFF_BASE ** min(n, MAX_BACKOFF_EXPONENT))
        )
        self.db.upsert(
            "source_cursors",
            {
                "source": source,
                "key": "global",
                "cursor": row.get("cursor"),
                "etag": row.get("etag"),
                "last_modified": row.get("last_modified"),
                "last_run_at": _iso(now),
                "next_due_at": _iso(now + timedelta(hours=hours)),
                "fail_count": n,
                "last_error": str(error)[:500],
            },
            pk=("source", "key"),
            overwrite={"last_run_at", "next_due_at", "fail_count", "last_error"},
        )


def cadences_from_config(config) -> dict[str, float]:
    """Resolve per-source cadence_hours from config/sources.yaml.

    Falls back to the ``defaults`` block, then to DEFAULT_CADENCE_HOURS.
    """
    table = config.load_yaml("sources") or {}
    sources = table.get("sources") or {}
    default_hours = float((table.get("defaults") or {}).get("cadence_hours") or DEFAULT_CADENCE_HOURS)
    out: dict[str, float] = {}
    for key, entry in sources.items():
        if isinstance(entry, dict) and entry.get("cadence_hours"):
            out[key] = float(entry["cadence_hours"])
        else:
            out[key] = default_hours
    return out


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes

        SYNCHRONIZE = 0x00100000
        WAIT_TIMEOUT = 0x00000102
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return False
        try:
            return k32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT  # running
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by someone else
    except OSError:
        return False


class SingleFlight:
    """Lockfile so only one process collects at a time.

    The lock file holds the owning pid; it is stale when the pid is dead or
    the file is older than ``LOCK_MAX_AGE_S`` — stale locks are broken on
    acquire. ``pid_alive`` and ``time_fn`` are injectable for tests.
    """

    def __init__(self, path, *, pid_alive=None, time_fn=time.time):
        self.path = Path(path)
        self._pid_alive = pid_alive or _pid_alive
        self._time_fn = time_fn
        self._acquired = False

    def acquire(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            for attempt in (1, 2):
                try:
                    # Atomic exclusive create: no exists()->unlink()->write()
                    # TOCTOU window between two racing acquirers.
                    fd = os.open(
                        str(self.path),
                        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    )
                except FileExistsError:
                    if not self._is_stale():
                        return False
                    logger.warning("breaking stale collect lock at {}", self.path)
                    try:
                        self.path.unlink()
                    except OSError:
                        return False  # someone else broke it first
                    continue  # one retry after stale-break
                try:
                    os.write(fd, str(os.getpid()).encode("utf-8"))
                finally:
                    os.close(fd)
                self._acquired = True
                return True
            return False  # still contested after one stale-break retry
        except OSError:
            logger.exception("single-flight lock failed at {}", self.path)
            return False

    def _is_stale(self) -> bool:
        try:
            raw = self.path.read_text(encoding="utf-8").strip()
            pid = int(raw) if raw else 0
        except (OSError, ValueError):
            return True  # unreadable/corrupt lock → break it
        if not self._pid_alive(pid):
            return True
        try:
            age = self._time_fn() - self.path.stat().st_mtime
        except OSError:
            return True
        return age > LOCK_MAX_AGE_S

    def release(self) -> None:
        if not self._acquired:
            return
        self._acquired = False
        try:
            raw = self.path.read_text(encoding="utf-8").strip()
            pid = int(raw) if raw else 0
        except (OSError, ValueError):
            return  # unreadable → don't guess; never unlink a lock we can't verify
        if pid != os.getpid():
            return  # foreign lock (stale-broken and re-acquired by another pid)
        try:
            self.path.unlink()
        except OSError:
            pass

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exc) -> bool:
        self.release()
        return False
