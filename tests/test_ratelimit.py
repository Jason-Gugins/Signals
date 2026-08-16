"""Tests for per-host token bucket and concurrency gate. No real sleeps."""

from __future__ import annotations

import threading

from src.core.ratelimit import RateLimiter, TokenBucket


class FakeClock:
    def __init__(self, start: float = 0.0):
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_first_acquire_is_free():
    clock = FakeClock()
    bucket = TokenBucket(rate_per_sec=2.0, clock=clock, sleep=clock.sleep)
    slept = bucket.acquire()
    assert slept == 0.0
    assert clock.sleeps == []


def test_rapid_acquires_at_two_per_sec_sleep_half_intervals():
    clock = FakeClock()
    bucket = TokenBucket(rate_per_sec=2.0, burst=1, clock=clock, sleep=clock.sleep)
    n = 5
    for _ in range(n):
        bucket.acquire()
    # first is free; remaining 4 wait 0.5s each
    assert abs(sum(clock.sleeps) - (n - 1) / 2) < 1e-9


def test_separate_hosts_do_not_block_each_other():
    clock = FakeClock()
    limiter = RateLimiter(default_rate=1.0, clock=clock, sleep=clock.sleep)
    limiter.wait("https://a.example/x")
    limiter.wait("https://b.example/x")
    assert clock.sleeps == []


def test_penalize_reduces_rate_then_recovers():
    clock = FakeClock()
    limiter = RateLimiter(default_rate=2.0, clock=clock, sleep=clock.sleep)
    url = "https://a.example/x"
    limiter.wait(url)  # consume the burst token
    limiter.penalize(url, seconds=10.0)
    limiter.wait(url)
    # halved rate = 1/s so next token costs ~1s, not 0.5s
    assert clock.sleeps
    assert abs(clock.sleeps[-1] - 1.0) < 1e-9
    clock.now += 10.0
    limiter.wait(url)  # consume whatever recovered
    limiter.wait(url)
    # back to 2/s → 0.5s
    assert abs(clock.sleeps[-1] - 0.5) < 1e-9


def test_slot_caps_concurrency():
    clock = FakeClock()
    limiter = RateLimiter(max_concurrency=2, clock=clock, sleep=clock.sleep)
    current = 0
    max_seen = 0
    lock = threading.Lock()
    entered = threading.Event()
    release = threading.Event()

    def hold():
        nonlocal current, max_seen
        with limiter.slot():
            with lock:
                current += 1
                max_seen = max(max_seen, current)
            entered.set()
            release.wait(timeout=2)
            with lock:
                current -= 1

    t1 = threading.Thread(target=hold)
    t2 = threading.Thread(target=hold)
    t1.start()
    t2.start()
    entered.wait(timeout=2)
    # third waiter should block until one exits
    blocked = threading.Event()

    def third():
        nonlocal current, max_seen
        blocked.set()
        with limiter.slot():
            with lock:
                current += 1
                max_seen = max(max_seen, current)

    t3 = threading.Thread(target=third)
    t3.start()
    blocked.wait(timeout=2)
    threading.Event().wait(0.05)
    with lock:
        assert current == 2
    release.set()
    t1.join(timeout=2)
    t2.join(timeout=2)
    t3.join(timeout=2)
    assert max_seen == 2
