from datetime import datetime, timedelta

from src.core.db import Database
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
