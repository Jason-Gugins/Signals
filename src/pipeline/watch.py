"""Watch scheduler for incremental collection."""

from __future__ import annotations

import time
from datetime import datetime

from loguru import logger


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


def watch_loop(
    orch,
    *,
    interval_minutes: int = 60,
    once: bool = False,
    sleep=time.sleep,
    now_fn=datetime.now,
    max_iterations: int | None = None,
) -> int:
    n = 0
    while True:
        try:
            adapters = orch._pick_adapters(None)
            due = due_sources(orch.db, adapters, now=now_fn())
            domains = sorted({d for _, ds in due for d in ds})
            orch.collect(domains=domains or None, force=False)
            orch.score(domains=domains or None)
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
