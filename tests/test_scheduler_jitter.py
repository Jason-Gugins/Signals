"""Task 5 (P3): prove crt.sh / wayback get the same ±5% jitter + backoff.

The scheduler is generic — there is no per-source special-casing — so these
tests pin the jitter behaviour for the two slowest sources explicitly:
record_success twice with different rng draws and assert the resulting
next_due_at lands inside the ±5% window AND differs between runs.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from src.core.db import Database
from src.pipeline.scheduler import Scheduler

NOW = datetime(2026, 9, 1, 12, 0, 0)
CADENCE_H = 336.0  # longest cadence: crt.sh and wayback
TOL = timedelta(seconds=2)


class SeqRng:
    """Deterministic rng: yields queued floats, then repeats the last one."""

    def __init__(self, *vals):
        self.vals = list(vals)

    def random(self):
        if not self.vals:
            return 0.5
        if len(self.vals) == 1:
            return self.vals[0]
        return self.vals.pop(0)


def _sched(tmp_path, rng=None) -> Scheduler:
    return Scheduler(Database(tmp_path / "s.db"), {"crtsh": CADENCE_H, "wayback": CADENCE_H}, rng=rng)


def _due(sched, source, rng_val, when=NOW):
    sched._rng = SeqRng(rng_val)
    sched.record_success(source, now=when)
    row = sched._cursor(source)
    return datetime.fromisoformat(row["next_due_at"])


def test_crtsh_wayback_jitter_within_pm5pct_window(tmp_path):
    for source in ("crtsh", "wayback"):
        sched = _sched(tmp_path / source)
        # rng draw r -> multiplier 1 + (2r-1)*0.05: 0.25 -> 0.975, 0.75 -> 1.025
        low = _due(sched, source, 0.25)
        high = _due(sched, source, 0.75)
        assert abs((low - NOW) - timedelta(hours=CADENCE_H * 0.975)) < TOL
        assert abs((high - NOW) - timedelta(hours=CADENCE_H * 1.025)) < TOL


def test_crtsh_wayback_due_differs_across_runs_by_jitter_seconds(tmp_path):
    for source in ("crtsh", "wayback"):
        sched = _sched(tmp_path / source)
        a = _due(sched, source, 0.25)
        b = _due(sched, source, 0.75)
        delta = b - a
        # jitter-eligible span for a 336h cadence is 10% of it = 33.6h
        eligible = timedelta(hours=CADENCE_H * 2 * 0.05)
        assert timedelta(0) < delta <= eligible + TOL


def test_crtsh_wayback_failure_backoff_uses_jittered_backoff_cadence(tmp_path):
    for source in ("crtsh", "wayback"):
        sched = _sched(tmp_path / source)
        sched._rng = SeqRng(0.25)  # jitter multiplier 0.975 throughout
        when = NOW
        for i in range(1, 4):
            sched.record_failure(source, now=when, error="x")
            when = when + timedelta(hours=1)
            row = sched._cursor(source)
            assert int(row["fail_count"]) == i
        # 3 consecutive failures -> 2**3 == 8x capped cadence, jittered by rng 0.25
        # (recorded on the 3rd failure, at NOW + 2h)
        due = datetime.fromisoformat(sched._cursor(source)["next_due_at"])
        expected = timedelta(hours=2 + CADENCE_H * 8 * 0.975)
        assert abs((due - NOW) - expected) < TOL


def test_record_success_resets_backoff_for_slow_sources(tmp_path):
    sched = _sched(tmp_path)
    sched.record_failure("crtsh", now=NOW, error="x")
    sched.record_failure("crtsh", now=NOW, error="x")
    assert int(sched._cursor("crtsh")["fail_count"]) == 2
    _due(sched, "crtsh", 0.5)
    row = sched._cursor("crtsh")
    assert int(row["fail_count"]) == 0
    due = datetime.fromisoformat(row["next_due_at"])
    assert abs((due - NOW) - timedelta(hours=CADENCE_H)) < TOL
