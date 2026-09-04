"""Runner stability: fanout-adapter isolation + cadence-scaled failure backoff.

Task 3: one fanout adapter raising inside run() must not abort every adapter
sorted after it — run() records the failure (stats + cursor) and keeps going,
mirroring the existing per-account try/except + _record_fail pattern.

Task 4: _record_fail backoff scales with the adapter's real cadence_hours
(wayback 8x336h, ATS 8x12h) instead of a hardcoded 24h.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.core.config import Config
from src.core.db import Database
from src.core.models import Account, Document
from src.core.rawstore import RawStore
from src.core.runlog import RunContext
from src.identity.registry import AccountRegistry
from src.pipeline.runner import CollectorRunner
from src.signals.store import SignalStore
from src.signals.taxonomy import Taxonomy
from src.sources.base import FetchTask, SignalCandidate, SourceAdapter


class OkFetch:
    """Fetcher that succeeds for any task."""

    def get(self, task, *, etag=None, last_modified=None):
        from src.core.http import FetchResult

        doc = Document(
            doc_id=f"d-{task.source}", source=task.source, url=task.url,
            domain=task.domain, body=b"ok", status=200,
        )
        return FetchResult(True, 200, doc, False, None, 1)


class FanoutBoom(SourceAdapter):
    key = "fanboom"
    tier = "http"
    fanout = True
    cadence_hours = 24
    requires = ()

    def plan(self, account, cursor):
        return [FetchTask(source=self.key, url="https://fanboom.test/all", domain=None)]

    def parse(self, doc, account, task_meta):
        return []


class AfterOk(SourceAdapter):
    key = "afterok"
    tier = "http"
    cadence_hours = 24
    requires = ()

    def plan(self, account, cursor):
        return [FetchTask(source=self.key, url=f"https://afterok.test/{account.domain}", domain=account.domain)]

    def parse(self, doc, account, task_meta):
        return [SignalCandidate("award", "2026-08-01", f"afterok:{account.domain}", title="A")]


def _runner(tmp_path, fetcher=None):
    """Build a CollectorRunner over a tmp DB (mirrors test_error_taxonomy)."""
    db = Database(tmp_path / "runner.db")
    cfg = Config()
    cfg.http.max_workers = 1
    cfg.http.respect_robots = False
    store = RawStore(db, tmp_path / "raw")
    tax = Taxonomy.load()
    ctx = RunContext(db, "collect")
    ctx.__enter__()
    runner = CollectorRunner(
        cfg, db, AccountRegistry(db), store, fetcher or OkFetch(),
        SignalStore(db, tax), tax, ctx,
    )
    return runner, db, ctx


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


# ── Task 3: fanout isolation in run() ───────────────────────────────────────


def test_fanout_failure_does_not_abort_later_adapters(tmp_path, monkeypatch):
    """A raising fanout adapter must not abort adapters sorted after it:
    run() stamps the failure (stats + cursor) and the next adapter still runs."""
    runner, db, ctx = _runner(tmp_path)

    real = CollectorRunner._run_fanout

    def _booming(self, adapter, accounts, stats, *, force, dry_run, now):
        if adapter.key == "fanboom":
            raise RuntimeError("boom")
        return real(self, adapter, accounts, stats, force=force, dry_run=dry_run, now=now)

    monkeypatch.setattr(CollectorRunner, "_run_fanout", _booming)

    acct = Account(domain="acme.com")
    stats = runner.run([FanoutBoom(), AfterOk()], [acct], force=True)
    ctx.__exit__(None, None, None)

    # (a) the SECOND adapter still ran and its signals persisted
    assert stats.by_source.get("afterok", {}).get("signals_new", 0) >= 1
    row = db.one("SELECT COUNT(*) AS n FROM signals WHERE source='afterok'")
    assert row["n"] >= 1
    # (b) stats reflect the fanout failure (global + per-source)
    assert stats.failed >= 1
    assert stats.by_source["fanboom"]["failed"] >= 1
    # (c) the fanout adapter's cursor was stamped via _record_fail
    cur = db.one(
        "SELECT fail_count, last_error FROM source_cursors "
        "WHERE source='fanboom' AND key='global'"
    )
    assert cur is not None
    assert int(cur["fail_count"]) == 1
    assert "boom" in (cur["last_error"] or "")
