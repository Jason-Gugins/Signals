from datetime import datetime, timedelta

from src.core.db import Database
from src.pipeline.scheduler import Scheduler
from src.pipeline.watch import due_sources, watch_loop
from src.sources.base import FetchTask, SourceAdapter


class A(SourceAdapter):
    key = "ok"

    def plan(self, account, cursor):
        return []

    def parse(self, doc, account, task_meta):
        return []


class FakeOrch:
    def __init__(self, db):
        self.db = db
        self.n = 0

    def _pick_adapters(self, sources):
        return [A()]

    def collect(self, **kw):
        self.n += 1

    def score(self, **kw):
        pass


def test_due_sources_and_loop(tmp_path):
    db = Database(tmp_path / "s.db")
    now = datetime(2026, 8, 16, 12, 0, 0)
    db.upsert("source_cursors", {"source": "ok", "key": "acme.com", "next_due_at": "2026-08-16T11:00:00"}, pk=("source", "key"))
    db.upsert("source_cursors", {"source": "ok", "key": "later.com", "next_due_at": "2026-08-17T00:00:00"}, pk=("source", "key"))
    due = due_sources(db, [A()], now=now)
    domains = due[0][1]
    assert "acme.com" in domains
    assert "later.com" not in domains

    sleeps = []
    orch = FakeOrch(db)
    n = watch_loop(orch, once=True, sleep=lambda s: sleeps.append(s), now_fn=lambda: now)
    assert n == 1 and sleeps == []

    n3 = watch_loop(orch, max_iterations=3, interval_minutes=1, sleep=lambda s: sleeps.append(s), now_fn=lambda: now)
    assert n3 == 3
    assert sleeps == [60, 60]

    class Boom(FakeOrch):
        def collect(self, **kw):
            raise RuntimeError("x")

    n = watch_loop(Boom(db), max_iterations=2, sleep=lambda s: None, now_fn=lambda: now)
    assert n == 2

    class Stop(FakeOrch):
        def collect(self, **kw):
            raise KeyboardInterrupt()

    # fresh db: the loop above recorded 'ok' as run, so it would be skipped
    stop_db = Database(tmp_path / "stop.db")
    assert watch_loop(Stop(stop_db), max_iterations=5, sleep=lambda s: None, now_fn=lambda: now) == 0


class First(SourceAdapter):
    key = "first"

    def plan(self, account, cursor):
        return []

    def parse(self, doc, account, task_meta):
        return []


class Second(SourceAdapter):
    key = "second"

    def plan(self, account, cursor):
        return []

    def parse(self, doc, account, task_meta):
        return []


class SpyScheduler(Scheduler):
    """Real scheduler with call recording for record_failure/record_success."""

    def __init__(self, db):
        super().__init__(db, {})
        self.fail_calls = []
        self.success_calls = []

    def record_failure(self, source, *, now, error=""):
        self.fail_calls.append(source)
        super().record_failure(source, now=now, error=error)

    def record_success(self, source, *, now):
        self.success_calls.append(source)
        super().record_success(source, now=now)


def test_watch_loop_isolates_failing_adapter(tmp_path):
    """First due adapter raising inside collect must not skip the second.

    (a) both adapters are collected, in order;
    (b) the raising adapter gets record_failure exactly once;
    (c) the clean adapter gets record_success.
    """

    class FlakyOrch(FakeOrch):
        def _pick_adapters(self, sources):
            return [First(), Second()]

        def collect(self, *, sources, **kw):
            self.n += 1
            self.collected.extend(sources)
            if "first" in sources:
                raise RuntimeError("first adapter boom")
            return None

    db = Database(tmp_path / "iso.db")
    now = datetime(2026, 8, 16, 12, 0, 0)
    orch = FlakyOrch(db)
    orch.collected = []
    sched = SpyScheduler(db)
    n = watch_loop(
        orch,
        once=True,
        sleep=lambda s: None,
        now_fn=lambda: now,
        scheduler=sched,
        lock_path=str(tmp_path / "iso.lock"),
    )
    assert n == 1
    # (a) second adapter still collected despite the first raising
    assert orch.collected == ["first", "second"]
    # (b) failure recorded for the raising source only, exactly once
    assert sched.fail_calls == ["first"]
    row = db.one("SELECT * FROM source_cursors WHERE source='first' AND key='global'")
    assert row["fail_count"] == 1
    # (c) clean adapter still got record_success
    assert sched.success_calls == ["second"]
    ok_row = db.one("SELECT * FROM source_cursors WHERE source='second' AND key='global'")
    assert ok_row["fail_count"] == 0
    assert ok_row["last_error"] is None
