"""Tests for the durable per-source scheduler (Task 11).

Injected clock everywhere — no sleeps, no real waits.
"""

from datetime import datetime, timedelta

from src.core.db import Database
from src.pipeline.scheduler import (
    LOCK_MAX_AGE_S,
    Scheduler,
    SingleFlight,
    cadences_from_config,
)
from src.pipeline.watch import watch_loop
from src.pipeline.runner import RunnerStats
from src.sources.base import SourceAdapter


NOW = datetime(2026, 8, 30, 12, 0, 0)


class SeqRng:
    """Deterministic rng: returns queued floats, then last one forever."""

    def __init__(self, *vals):
        self.vals = list(vals)

    def random(self):
        if not self.vals:
            return 0.5
        if len(self.vals) == 1:
            return self.vals[0]
        return self.vals.pop(0)


class A(SourceAdapter):
    key = "ok"

    def plan(self, account, cursor):
        return []

    def parse(self, doc, account, task_meta):
        return []


class B(SourceAdapter):
    key = "slow"

    def plan(self, account, cursor):
        return []

    def parse(self, doc, account, task_meta):
        return []


def make_sched(cadences=None, rng=None):
    db = Database(":memory:")
    return db, Scheduler(db, cadences or {"ok": 24.0}, rng=rng or SeqRng(0.5))


# ── due decision ────────────────────────────────────────────────────────────


def test_never_run_source_is_due():
    _db, s = make_sched()
    assert [a.key for a in s.decide_due([A()], now=NOW)] == ["ok"]


def test_cadence_elapsed_vs_not():
    db, s = make_sched(rng=SeqRng(0.5))  # 0.5 → zero jitter
    s.record_success("ok", now=NOW)
    row = db.one("SELECT * FROM source_cursors WHERE source='ok' AND key='global'")
    assert row["fail_count"] == 0 and row["last_run_at"] == NOW.isoformat()
    assert s.decide_due([A()], now=NOW + timedelta(hours=23)) == []
    assert [a.key for a in s.decide_due([A()], now=NOW + timedelta(hours=24))] == ["ok"]


def test_missing_next_due_at_means_due():
    db, s = make_sched()
    db.upsert(
        "source_cursors",
        {"source": "ok", "key": "global", "last_run_at": NOW.isoformat(), "next_due_at": None},
        pk=("source", "key"),
    )
    assert [a.key for a in s.decide_due([A()], now=NOW)] == ["ok"]


def test_catchup_after_downtime():
    """Cadence elapsed while the process was down → immediately due."""
    db, s = make_sched(rng=SeqRng(0.5))
    s.record_success("ok", now=NOW)
    assert [a.key for a in s.decide_due([A()], now=NOW + timedelta(days=30))] == ["ok"]


# ── jitter ──────────────────────────────────────────────────────────────────


def test_jitter_bounds():
    s = Database(":memory:"), None
    db = s[0]
    hi = Scheduler(db, {"ok": 24.0}, rng=SeqRng(1.0))
    lo = Scheduler(db, {"ok": 24.0}, rng=SeqRng(0.0))
    assert hi.jittered(24.0) == 24.0 * 1.05
    assert lo.jittered(24.0) == 24.0 * 0.95
    mid = Scheduler(db, {"ok": 24.0}, rng=SeqRng(0.5))
    assert mid.jittered(24.0) == 24.0


def test_next_due_at_is_jittered():
    db, s = make_sched(rng=SeqRng(1.0))  # +5%
    s.record_success("ok", now=NOW)
    row = db.one("SELECT * FROM source_cursors WHERE source='ok' AND key='global'")
    due = datetime.fromisoformat(row["next_due_at"])
    assert due == NOW + timedelta(hours=24 * 1.05)


# ── backoff ─────────────────────────────────────────────────────────────────


def test_backoff_escalation_capped_at_8x():
    db, s = make_sched(rng=SeqRng(0.5))
    for _ in range(5):
        s.record_failure("ok", now=NOW, error="boom")
    assert s.effective_cadence("ok") == 24.0 * 8  # 2**3 cap
    row = db.one("SELECT * FROM source_cursors WHERE source='ok' AND key='global'")
    assert row["fail_count"] == 5 and row["last_error"] == "boom"
    due = datetime.fromisoformat(row["next_due_at"])
    assert due == NOW + timedelta(hours=24 * 8)


def test_backoff_steps():
    _db, s = make_sched(rng=SeqRng(0.5))
    assert s.effective_cadence("ok") == 24.0  # no failures yet
    s.record_failure("ok", now=NOW, error="e1")
    assert s.effective_cadence("ok") == 24.0 * 2
    s.record_failure("ok", now=NOW, error="e2")
    assert s.effective_cadence("ok") == 24.0 * 4
    s.record_failure("ok", now=NOW, error="e3")
    assert s.effective_cadence("ok") == 24.0 * 8


def test_success_resets_backoff():
    _db, s = make_sched(rng=SeqRng(0.5))
    s.record_failure("ok", now=NOW, error="e1")
    s.record_failure("ok", now=NOW, error="e2")
    s.record_success("ok", now=NOW)
    assert s.effective_cadence("ok") == 24.0


def test_unknown_source_uses_default_cadence():
    _db, s = make_sched()
    assert s.cadence("never_seen") == 24.0


# ── single-flight lock ──────────────────────────────────────────────────────


def test_lock_acquire_release(tmp_path):
    lock = SingleFlight(tmp_path / "collect.lock", pid_alive=lambda pid: True)
    assert lock.acquire() is True
    assert (tmp_path / "collect.lock").exists()
    # second holder cannot acquire
    other = SingleFlight(tmp_path / "collect.lock", pid_alive=lambda pid: True)
    assert other.acquire() is False
    lock.release()
    assert not (tmp_path / "collect.lock").exists()
    assert other.acquire() is True
    other.release()


def test_lock_stale_break_dead_pid(tmp_path):
    path = tmp_path / "collect.lock"
    path.write_text("999999")
    lock = SingleFlight(path, pid_alive=lambda pid: False)
    assert lock.acquire() is True  # dead pid → broken and taken


def test_lock_stale_break_old_mtime(tmp_path):
    import os

    path = tmp_path / "collect.lock"
    path.write_text("12345")
    old = 1_000_000_000.0  # epoch, way older than 24h
    os.utime(path, (old, old))
    ticks = {"t": old + 25 * 3600}
    lock = SingleFlight(
        path,
        pid_alive=lambda pid: True,
        time_fn=lambda: ticks["t"],
    )
    assert lock.acquire() is True  # > LOCK_MAX_AGE_S old → broken


def test_lock_fresh_not_broken(tmp_path):
    import os

    path = tmp_path / "collect.lock"
    path.write_text("12345")
    now = 1_000_000_000.0
    os.utime(path, (now, now))
    lock = SingleFlight(path, pid_alive=lambda pid: True, time_fn=lambda: now + 60)
    assert lock.acquire() is False


def test_lock_context_manager(tmp_path):
    with SingleFlight(tmp_path / "collect.lock", pid_alive=lambda pid: True) as got:
        assert got is True
    assert not (tmp_path / "collect.lock").exists()


def test_lock_max_age_constant():
    assert LOCK_MAX_AGE_S == 24 * 3600


# ── cadences from config ────────────────────────────────────────────────────


def test_cadences_from_config():
    class Cfg:
        def load_yaml(self, name):
            assert name == "sources"
            return {
                "defaults": {"cadence_hours": 24},
                "sources": {"ok": {"cadence_hours": 12}, "bare": {"enabled": True}},
            }

    cad = cadences_from_config(Cfg())
    assert cad["ok"] == 12.0
    assert cad["bare"] == 24.0  # falls back to defaults


# ── watch_loop integration ──────────────────────────────────────────────────


class FakeOrch:
    def __init__(self, db, stats=None):
        self.db = db
        self.config = None
        self.calls = []
        self.stats = stats

    def _pick_adapters(self, sources):
        return [A(), B()]

    def collect(self, **kw):
        self.calls.append(kw)
        return self.stats

    def score(self, **kw):
        self.calls.append({"score": kw.get("domains")})


def test_watch_loop_collects_only_due_sources():
    db, s = make_sched(rng=SeqRng(0.5))
    s.record_success("slow", now=NOW)  # not due yet
    orch = FakeOrch(db)
    watch_loop(
        orch, once=True, sleep=lambda _: None, now_fn=lambda: NOW, scheduler=s,
        lock_path=None,  # disable single-flight for this test
    )
    collects = [c for c in orch.calls if "sources" in c]
    assert len(collects) == 1
    assert collects[0]["sources"] == ["ok"]  # only the due source


def test_watch_loop_records_failure_for_backoff():
    db, s = make_sched(rng=SeqRng(0.5))
    stats = RunnerStats()
    stats._src("ok")["failed"] = 2
    orch = FakeOrch(db, stats=stats)
    watch_loop(
        orch, once=True, sleep=lambda _: None, now_fn=lambda: NOW, scheduler=s, lock_path=None
    )
    row = db.one("SELECT * FROM source_cursors WHERE source='ok' AND key='global'")
    assert row["fail_count"] == 1
    # due again only after the 2x backoff cadence
    assert s.decide_due([A()], now=NOW + timedelta(hours=24)) == []
    assert [a.key for a in s.decide_due([A()], now=NOW + timedelta(hours=48))] == ["ok"]


def test_watch_loop_success_schedules_next_tick():
    db, s = make_sched(rng=SeqRng(0.5))
    orch = FakeOrch(db, stats=RunnerStats())
    watch_loop(
        orch, once=True, sleep=lambda _: None, now_fn=lambda: NOW, scheduler=s, lock_path=None
    )
    assert s.decide_due([A()], now=NOW + timedelta(hours=23)) == []
    assert [a.key for a in s.decide_due([A()], now=NOW + timedelta(hours=24))] == ["ok"]


def test_watch_loop_lock_blocks_collect(tmp_path):
    db, _s = make_sched()
    path = tmp_path / "collect.lock"
    path.write_text("1")
    orch = FakeOrch(db)
    lock_path = str(path)
    watch_loop(
        orch, once=True, sleep=lambda _: None, now_fn=lambda: NOW,
        scheduler=Scheduler(db, {"ok": 24.0, "slow": 24.0}),
        lock_path=lock_path,
        pid_alive=lambda pid: True,
    )
    assert not [c for c in orch.calls if "sources" in c]  # blocked by lock


def test_watch_loop_single_flight_signature(tmp_path):
    """lock_path + injected pid check flows through the loop kwargs."""
    db, s = make_sched()
    orch = FakeOrch(db, stats=RunnerStats())
    watch_loop(
        orch, once=True, sleep=lambda _: None, now_fn=lambda: NOW, scheduler=s,
        lock_path=str(tmp_path / "c.lock"), pid_alive=lambda pid: True,
    )
    assert [c for c in orch.calls if "sources" in c]  # collected, lock was free
