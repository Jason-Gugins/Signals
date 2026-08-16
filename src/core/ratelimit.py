"""Per-host token-bucket rate limiter with a global concurrency gate."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Iterator
from urllib.parse import urlparse


class TokenBucket:
    def __init__(
        self,
        rate_per_sec: float,
        burst: int | None = None,
        clock=time.monotonic,
        sleep=time.sleep,
    ):
        self.rate_per_sec = float(rate_per_sec)
        self.burst = float(burst if burst is not None else 1.0)
        self.clock = clock
        self.sleep = sleep
        self._tokens = self.burst
        self._updated = clock()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = self.clock()
        elapsed = max(0.0, now - self._updated)
        self._tokens = min(self.burst, self._tokens + elapsed * self.rate_per_sec)
        self._updated = now

    def acquire(self, n: int = 1) -> float:
        slept = 0.0
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= n:
                    self._tokens -= n
                    return slept
                need = n - self._tokens
                wait = need / self.rate_per_sec if self.rate_per_sec > 0 else 0.0
            if wait > 0:
                self.sleep(wait)
                slept += wait
            else:
                return slept


class RateLimiter:
    def __init__(
        self,
        default_rate: float = 1.0,
        per_host: dict[str, float] | None = None,
        max_concurrency: int = 6,
        clock=time.monotonic,
        sleep=time.sleep,
    ):
        self.default_rate = default_rate
        self.per_host = dict(per_host or {})
        self.clock = clock
        self.sleep = sleep
        self._buckets: dict[str, TokenBucket] = {}
        self._base_rates: dict[str, float] = {}
        self._penalty_until: dict[str, float] = {}
        self._sema = threading.Semaphore(max_concurrency)
        self._lock = threading.Lock()

    def _host(self, url: str) -> str:
        return (urlparse(url).hostname or url).lower()

    def _bucket(self, host: str) -> TokenBucket:
        with self._lock:
            until = self._penalty_until.get(host)
            if until is not None and self.clock() >= until:
                self._penalty_until.pop(host, None)
                if host in self._buckets:
                    self._buckets[host].rate_per_sec = self._base_rates[host]
            if host not in self._buckets:
                rate = self.per_host.get(host, self.default_rate)
                self._base_rates[host] = rate
                self._buckets[host] = TokenBucket(
                    rate_per_sec=rate, clock=self.clock, sleep=self.sleep
                )
            return self._buckets[host]

    def wait(self, url: str) -> float:
        host = self._host(url)
        return self._bucket(host).acquire()

    def penalize(self, url: str, seconds: float) -> None:
        host = self._host(url)
        bucket = self._bucket(host)
        with self._lock:
            base = self._base_rates[host]
            bucket.rate_per_sec = base / 2.0
            self._penalty_until[host] = self.clock() + seconds

    @contextmanager
    def slot(self) -> Iterator[None]:
        self._sema.acquire()
        try:
            yield
        finally:
            self._sema.release()
