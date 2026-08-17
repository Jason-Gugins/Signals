"""Resilient concurrent collector runner."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from loguru import logger

from src.core.models import Account, Document
from src.core.runlog import RunContext
from src.signals.normalize import normalize_batch
from src.sources.base import FetchTask, SourceAdapter


@dataclass
class RunnerStats:
    tasks: int = 0
    fetched: int = 0
    cached: int = 0
    failed: int = 0
    skipped: int = 0
    candidates: int = 0
    signals_new: int = 0
    by_source: dict = field(default_factory=dict)

    def _src(self, key: str) -> dict:
        return self.by_source.setdefault(
            key, {"tasks": 0, "fetched": 0, "cached": 0, "failed": 0, "skipped": 0, "candidates": 0, "signals_new": 0}
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


class CollectorRunner:
    def __init__(self, config, db, registry, store, fetcher, signal_store, taxonomy, ctx: RunContext, browser=None):
        self.config = config
        self.db = db
        self.registry = registry
        self.store = store
        self.fetcher = fetcher
        self.signal_store = signal_store
        self.taxonomy = taxonomy
        self.ctx = ctx
        self.browser = browser

    def run(
        self,
        adapters: list[SourceAdapter],
        accounts: list[Account],
        *,
        force: bool = False,
        dry_run: bool = False,
        max_passes: int = 2,
        limit_per_source: int | None = None,
    ) -> RunnerStats:
        stats = RunnerStats()
        now = _now()
        for adapter in adapters:
            eligible = []
            for account in accounts:
                if not _requires_met(adapter, account):
                    stats.skipped += 1
                    stats._src(adapter.key)["skipped"] += 1
                    continue
                cur = self._cursor(adapter.key, _ckey(adapter, account))
                if not force and cur:
                    if int(cur.get("fail_count") or 0) >= 5:
                        stats.skipped += 1
                        stats._src(adapter.key)["skipped"] += 1
                        continue
                    due = cur.get("next_due_at")
                    if due and due > _iso(now):
                        stats.skipped += 1
                        stats._src(adapter.key)["skipped"] += 1
                        continue
                eligible.append(account)
            if getattr(adapter, "fanout", False) and eligible:
                self._run_fanout(adapter, eligible, stats, force=force, dry_run=dry_run, now=now)
                continue
            for account in eligible:
                try:
                    self._run_pair(adapter, account, stats, force=force, dry_run=dry_run, max_passes=max_passes, limit_per_source=limit_per_source, now=now)
                except Exception as exc:
                    logger.exception("adapter {} failed for {}", adapter.key, account.domain)
                    self._record_fail(adapter.key, account.domain, exc)
                    stats.failed += 1
                    stats._src(adapter.key)["failed"] += 1
                    self.ctx.bump(errors=1)
        return stats

    def _run_pair(self, adapter, account, stats, *, force, dry_run, max_passes, limit_per_source, now):
        key = _ckey(adapter, account)
        cursor_row = self._cursor(adapter.key, key) or {}
        cursor = cursor_row.get("cursor")
        for pass_i in range(max_passes):
            tasks = adapter.plan(account, cursor)
            if limit_per_source:
                tasks = tasks[:limit_per_source]
            if dry_run:
                stats.tasks += len(tasks)
                stats._src(adapter.key)["tasks"] += len(tasks)
                logger.info("dry_run {} {} {} tasks", adapter.key, account.domain, len(tasks))
                return
            results = self._execute_tasks(adapter, tasks, cursor_row, stats)
            last_doc = None
            all_cands = []
            for task, result in results:
                if result is None:
                    continue
                if result.status == 304:
                    stats.cached += 1
                    stats._src(adapter.key)["cached"] += 1
                    continue
                if not result.ok or result.doc is None:
                    raise RuntimeError(result.error or f"fetch failed {result.status}")
                stats.fetched += 1
                stats._src(adapter.key)["fetched"] += 1
                self.ctx.bump(documents=1)
                last_doc = result.doc
                cands = adapter.parse(result.doc, account, dict(task.meta or {}))
                all_cands.extend(cands)
            stats.candidates += len(all_cands)
            stats._src(adapter.key)["candidates"] += len(all_cands)
            new_n = self._persist(account, adapter.key, all_cands, last_doc)
            stats.signals_new += new_n
            stats._src(adapter.key)["signals_new"] += new_n
            if last_doc is not None:
                cursor = adapter.next_cursor(last_doc, all_cands)
                self._record_success(adapter, key, cursor, last_doc, now)
            elif not results:
                self._record_success(adapter, key, cursor, None, now)
                break
            if pass_i + 1 < max_passes and cursor and last_doc is not None:
                continue
            break

    def _run_fanout(self, adapter, accounts, stats, *, force, dry_run, now):
        # one plan from the first account; parse per account
        cursor_row = self._cursor(adapter.key, "global") or {}
        tasks = adapter.plan(accounts[0], cursor_row.get("cursor"))
        if dry_run:
            stats.tasks += len(tasks)
            return
        results = self._execute_tasks(adapter, tasks, cursor_row, stats)
        last_doc = None
        for task, result in results:
            if result is None or result.status == 304:
                if result and result.status == 304:
                    stats.cached += 1
                continue
            if not result.ok or result.doc is None:
                raise RuntimeError(result.error or "fanout fetch failed")
            stats.fetched += 1
            last_doc = result.doc
            for account in accounts:
                cands = adapter.parse(result.doc, account, dict(task.meta or {}))
                stats.candidates += len(cands)
                new_n = self._persist(account, adapter.key, cands, result.doc)
                stats.signals_new += new_n
        if last_doc is not None:
            self._record_success(adapter, "global", adapter.next_cursor(last_doc, []), last_doc, now)

    def _execute_tasks(self, adapter, tasks, cursor_row, stats) -> list[tuple[FetchTask, object]]:
        stats.tasks += len(tasks)
        stats._src(adapter.key)["tasks"] += len(tasks)
        etag = cursor_row.get("etag")
        last_mod = cursor_row.get("last_modified")
        out: list[tuple[FetchTask, object]] = []
        if adapter.tier == "browser":
            for task in tasks:
                out.append((task, self._fetch_one(task, etag, last_mod)))
            return out
        workers = max(1, int(self.config.http.max_workers or 1))
        if len(tasks) <= 1 or workers == 1:
            return [(t, self._fetch_one(t, etag, last_mod)) for t in tasks]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(self._fetch_one, t, etag, last_mod): t for t in tasks}
            for fut in as_completed(futs):
                task = futs[fut]
                out.append((task, fut.result()))
        return out

    def _fetch_one(self, task, etag, last_mod):
        return self.fetcher.get(task, etag=etag, last_modified=last_mod)

    def _persist(self, account, source, cands, doc) -> int:
        if not cands:
            return 0
        now = _iso(_now())
        raw_ref = doc.doc_id if doc else None
        valid, _rej = normalize_batch(cands, account=account, source=source, taxonomy=self.taxonomy, now=now, raw_ref=raw_ref)
        new, _upd = self.signal_store.upsert_many(valid)
        self.ctx.bump(signals_new=new)
        return new

    def _cursor(self, source: str, key: str) -> Optional[dict]:
        return self.db.one("SELECT * FROM source_cursors WHERE source=? AND key=?", (source, key))

    def _record_success(self, adapter, key, cursor, doc, now):
        due = _iso(now + timedelta(hours=int(adapter.cadence_hours or 24)))
        self.db.upsert(
            "source_cursors",
            {
                "source": adapter.key,
                "key": key,
                "cursor": cursor,
                "etag": getattr(doc, "etag", None) if doc else None,
                "last_modified": getattr(doc, "last_modified", None) if doc else None,
                "last_run_at": _iso(now),
                "next_due_at": due,
                "fail_count": 0,
                "last_error": None,
            },
            pk=("source", "key"),
        )

    def _record_fail(self, source, key, exc):
        row = self._cursor(source, key) or {}
        n = int(row.get("fail_count") or 0) + 1
        self.db.upsert(
            "source_cursors",
            {
                "source": source,
                "key": key,
                "cursor": row.get("cursor"),
                "etag": row.get("etag"),
                "last_modified": row.get("last_modified"),
                "last_run_at": row.get("last_run_at"),
                "next_due_at": row.get("next_due_at"),
                "fail_count": n,
                "last_error": str(exc)[:500],
            },
            pk=("source", "key"),
        )


def _ckey(adapter, account) -> str:
    return "global" if getattr(adapter, "fanout", False) else account.domain


def _requires_met(adapter, account) -> bool:
    for field_name in adapter.requires or ():
        if not getattr(account, field_name, None):
            return False
    return True
