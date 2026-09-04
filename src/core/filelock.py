"""Best-effort cross-process file lock for shared-JSON read-modify-write.

Mirrors the house SingleFlight pattern (``src/pipeline/scheduler.py``): an
atomic ``O_CREAT|O_EXCL`` lockfile holding the owning pid, stale locks
(dead pid or older than ``stale_s``) broken on acquire, own-lock-only
release.

Unlike SingleFlight this context manager is FAIL-OPEN: on timeout (or any
acquire error) it logs a warning and yields ``False`` instead of raising.
The JSON state files it guards (trend stats, the empty-slug log) are
best-effort state — the lock is an optimization over the previous
unconditional last-writer-wins race, never a reason to skip an update.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Union

from loguru import logger

__all__ = ["exclusive_lock"]

_LockPath = Union[str, os.PathLike]

# Bound on stale-break retries: a lock we cannot actually remove (e.g. a
# Windows permission race) must fall through to the timeout, not spin.
_MAX_STALE_BREAKS = 3


def _pid_alive(pid: int) -> bool:
    """True when *pid* is a running process (scheduler.py semantics)."""
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


def _is_stale(lock_path: Path, stale_s: float) -> bool:
    try:
        raw = lock_path.read_text(encoding="utf-8").strip()
        pid = int(raw) if raw else 0
    except (OSError, ValueError):
        return True  # unreadable/corrupt lock → break it
    if not _pid_alive(pid):
        return True
    try:
        age = time.time() - lock_path.stat().st_mtime
    except OSError:
        return True
    return age > stale_s


@contextmanager
def exclusive_lock(
    path: _LockPath,
    *,
    timeout_s: float = 5.0,
    poll_s: float = 0.05,
    stale_s: float = 30.0,
) -> Iterator[bool]:
    """Try to take an exclusive lockfile next to *path* for the duration.

    The lockfile is ``<path>.lock``; *path* is the guarded state file (the
    JSON being read-modify-written), so each state file gets its own lock.

    Yields ``True`` when the lock was acquired, ``False`` on timeout or
    acquire error. Fail-open by design: the caller proceeds unguarded,
    exactly as before this lock existed.
    """
    target = Path(path)
    lock_path = target.with_name(target.name + ".lock")
    acquired = False
    try:
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.exception("state lock dir creation failed at {}", lock_path)
        else:
            deadline = time.monotonic() + timeout_s
            stale_breaks = 0
            while True:
                try:
                    # Atomic exclusive create: no exists()->unlink()->write()
                    # TOCTOU window between two racing acquirers.
                    fd = os.open(
                        str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY
                    )
                except FileExistsError:
                    if (
                        stale_breaks < _MAX_STALE_BREAKS
                        and _is_stale(lock_path, stale_s)
                    ):
                        stale_breaks += 1
                        logger.warning("breaking stale state lock at {}", lock_path)
                        try:
                            lock_path.unlink()
                        except OSError:
                            pass  # someone else broke it first — just retry
                        continue  # immediate retry after a stale-break
                    if time.monotonic() >= deadline:
                        logger.warning(
                            "state lock {} busy after {:.1f}s — proceeding "
                            "unguarded (fail-open)",
                            lock_path,
                            timeout_s,
                        )
                        break
                    time.sleep(poll_s)
                    continue
                except OSError:
                    logger.exception("state lock acquire failed at {}", lock_path)
                    break
                wrote = False
                try:
                    os.write(fd, str(os.getpid()).encode("utf-8"))
                    wrote = True
                except OSError:
                    pass  # handled below, after the fd is closed
                finally:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                if wrote:
                    acquired = True
                else:
                    # Fail-open: an unwritable pid must not raise out of
                    # __enter__ nor orphan a lockfile that blocks others.
                    # (Unlink only after close — Windows cannot unlink an
                    # open file.)
                    logger.warning(
                        "state lock {} pid write failed — proceeding unguarded",
                        lock_path,
                    )
                    try:
                        lock_path.unlink()
                    except OSError:
                        pass
                break
        yield acquired
    finally:
        if acquired:
            # Own-lock-only release: only unlink while the lockfile still
            # holds OUR pid — a hold longer than stale_s can be legitimately
            # stale-broken and re-acquired by another process meanwhile.
            try:
                if lock_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                    lock_path.unlink()
            except OSError:
                pass
