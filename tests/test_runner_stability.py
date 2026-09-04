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


# ── Task 4: failure backoff scales with the adapter's real cadence ──────────


def _due_hours_from_now(row):
    due = _utc(datetime.fromisoformat(row["next_due_at"]))
    return (due - datetime.now(timezone.utc)).total_seconds() / 3600


def test_record_fail_first_penalty_scales_with_cadence(tmp_path):
    """First failure (no prior next_due_at): penalty = cadence_hours *
    multiplier. A 336h-cadence source (wayback) gets 336*2h, not 48h."""
    runner, db, ctx = _runner(tmp_path)
    runner._record_fail("wayback", "d", RuntimeError("x"), cadence_hours=336)
    ctx.__exit__(None, None, None)

    row = db.one("SELECT next_due_at FROM source_cursors WHERE source='wayback' AND key='d'")
    assert row is not None
    hours = _due_hours_from_now(row)
    # RuntimeError unclassified -> 'other' (multiplier 2): 336 * 2 = 672h
    assert hours == pytest.approx(336 * 2, abs=1.0)


def test_record_fail_default_cadence_is_24h(tmp_path):
    """Without the kwarg the penalty stays on the 24h scale (24*2 = 48h)."""
    runner, db, ctx = _runner(tmp_path)
    runner._record_fail("ok", "d", RuntimeError("x"))
    ctx.__exit__(None, None, None)

    row = db.one("SELECT next_due_at FROM source_cursors WHERE source='ok' AND key='d'")
    assert row is not None
    hours = _due_hours_from_now(row)
    assert hours == pytest.approx(24 * 2, abs=1.0)


def test_record_fail_cap_scales_with_cadence(tmp_path):
    """The 8x cap is computed against the REAL cadence: a 336h source caps at
    floor + 8*336h, a 24h source at floor + 8 days; the 336h cap must be
    strictly later than the 24h cap (relationship, not absolute instants)."""
    runner, db, ctx = _runner(tmp_path)
    stale = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    # Seed deep-failed cursors so prev + 2^n * 2 days far exceeds any cap.
    db.upsert(
        "source_cursors",
        {"source": "wayback", "key": "d336", "fail_count": 10, "next_due_at": stale},
        pk=("source", "key"),
    )
    db.upsert(
        "source_cursors",
        {"source": "ok", "key": "d24", "fail_count": 10, "next_due_at": stale},
        pk=("source", "key"),
    )
    runner._record_fail("wayback", "d336", RuntimeError("x"), cadence_hours=336)
    runner._record_fail("ok", "d24", RuntimeError("x"), cadence_hours=24)
    ctx.__exit__(None, None, None)

    r336 = db.one("SELECT next_due_at FROM source_cursors WHERE source='wayback' AND key='d336'")
    r24 = db.one("SELECT next_due_at FROM source_cursors WHERE source='ok' AND key='d24'")
    due336 = _utc(datetime.fromisoformat(r336["next_due_at"]))
    due24 = _utc(datetime.fromisoformat(r24["next_due_at"]))
    now = datetime.now(timezone.utc)
    # 336h case: capped at floor + 8*336h (2688h)
    assert (due336 - now) <= timedelta(hours=8 * 336 + 1)
    # ... and strictly beyond the 24h-cadence cap (8 days)
    assert (due336 - now) > timedelta(hours=8 * 24 + 1)
    # 24h case: capped at floor + 8 days
    assert (due24 - now) <= timedelta(hours=8 * 24 + 1)
    # relationship: the 336h cap lands far later than the 24h cap
    assert due336 > due24


def test_run_passes_adapter_cadence_to_record_fail(tmp_path):
    """The per-account failure path passes the adapter's real cadence: a
    12h-cadence source's first failure is 12*2 = 24h out, not the old
    hardcoded 24*2 = 48h."""
    class Slow12(SourceAdapter):
        key = "slow12"
        tier = "http"
        cadence_hours = 12
        requires = ()

        def plan(self, account, cursor):
            return [FetchTask(source=self.key, url=f"https://slow12.test/{account.domain}", domain=account.domain)]

        def parse(self, doc, account, task_meta):
            raise RuntimeError("x")

    runner, db, ctx = _runner(tmp_path)
    stats = runner.run([Slow12()], [Account(domain="acme.com")], force=True)
    ctx.__exit__(None, None, None)

    assert stats.failed >= 1
    row = db.one("SELECT next_due_at FROM source_cursors WHERE source='slow12' AND key='acme.com'")
    assert row is not None
    hours = _due_hours_from_now(row)
    assert hours == pytest.approx(12 * 2, abs=1.0)
