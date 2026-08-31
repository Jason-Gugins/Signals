"""Watch scheduler for incremental collection.

The loop asks the durable :class:`~src.pipeline.scheduler.Scheduler` which
sources are due (per-source cadence, last-success from the DB, jitter,
failure backoff) and collects only those. A single-flight lockfile
(``data/state/collect.lock``) prevents overlapping collects across
processes; a stale lock is broken automatically.

Note: calling ``collect`` per-source from OS cron remains the recommended
production setup — this loop is the durable in-process alternative.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from loguru import logger

from src.pipeline.scheduler import Scheduler, SingleFlight, cadences_from_config


def due_sources(db, adapters, *, now: datetime) -> list[tuple]:
    iso = now.replace(microsecond=0).isoformat()
    out = []
    for adapter in adapters:
        rows = db.query(
            "SELECT key FROM source_cursors WHERE source=? AND (next_due_at IS NULL OR next_due_at <= ?)",
            (adapter.key, iso),
        )
        domains = [r["key"] for r in rows if r["key"] and r["key"] != "global"]
        if not rows:
            # no cursor yet — all accounts are due; caller supplies them via collect()
            out.append((adapter, []))
        else:
            out.append((adapter, domains))
    return out


def _record_outcomes(scheduler, due, stats, *, now: datetime) -> None:
    """Translate per-source RunnerStats into scheduler success/failure."""
    for adapter in due:
        try:
            src = None
            if stats is not None and hasattr(stats, "_src"):
                src = stats._src(adapter.key)
            if src and src.get("failed") and not (src.get("fetched") or src.get("cached")):
                scheduler.record_failure(adapter.key, now=now, error="collection failures")
            else:
                scheduler.record_success(adapter.key, now=now)
        except Exception:
            logger.exception("scheduler bookkeeping failed for {}", adapter.key)


def watch_loop(
    orch,
    *,
    interval_minutes: int = 60,
    once: bool = False,
    sleep=time.sleep,
    now_fn=datetime.now,
    max_iterations: int | None = None,
    scheduler: Scheduler | None = None,
    lock_path: str | Path | None = None,
    pid_alive=None,
) -> int:
    if scheduler is None:
        config = getattr(orch, "config", None)
        cadences = cadences_from_config(config) if config is not None else {}
        scheduler = Scheduler(orch.db, cadences)
    if lock_path is None:
        config = getattr(orch, "config", None)
        base = getattr(getattr(config, "storage", None), "session_dir", None) or "data/state"
        lock_path = Path(base) / "collect.lock"
    n = 0
    while True:
        try:
            adapters = orch._pick_adapters(None)
            now = now_fn()
            due = scheduler.decide_due(adapters, now=now)
            if due:
                pairs = due_sources(orch.db, due, now=now)
                domains = sorted({d for _, ds in pairs for d in ds})
                lock = SingleFlight(lock_path, pid_alive=pid_alive, time_fn=now.timestamp)
                if not lock.acquire():
                    logger.info("collect already running (single-flight lock held) — skipping tick")
                else:
                    try:
                        # Per-source isolation: one adapter raising must not
                        # skip the rest of this tick's due sources.
                        collected_any = False
                        for adapter, src_domains in pairs:
                            try:
                                stats = orch.collect(
                                    sources=[adapter.key], domains=src_domains or None, force=False
                                )
                            except Exception:
                                logger.exception("watch: adapter {} failed", adapter.key)
                                try:
                                    scheduler.record_failure(
                                        adapter.key, now=now_fn(), error="collect error"
                                    )
                                except Exception:
                                    logger.exception(
                                        "scheduler bookkeeping failed for {}", adapter.key
                                    )
                                continue
                            collected_any = True
                            try:
                                _record_outcomes(scheduler, [adapter], stats, now=now_fn())
                            except Exception:
                                logger.exception("scheduler bookkeeping failed for {}", adapter.key)
                        if collected_any:
                            orch.score(domains=domains or None)
                    finally:
                        lock.release()
            else:
                logger.debug("no sources due")
            try:
                from src.pipeline.watchlist import check
                from src.signals.taxonomy import Taxonomy

                check(orch.db, taxonomy=Taxonomy.load(), today=now_fn().date())
            except Exception:
                logger.exception("watchlist check failed")
        except KeyboardInterrupt:
            logger.info("watch stopped after {} iterations", n)
            return n
        except Exception:
            logger.exception("watch iteration failed")
        n += 1
        if once or (max_iterations is not None and n >= max_iterations):
            return n
        sleep(interval_minutes * 60)
