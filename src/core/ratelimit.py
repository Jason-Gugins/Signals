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
        per_source: dict[str, float] | None = None,
        max_concurrency: int = 6,
        clock=time.monotonic,
        sleep=time.sleep,
    ):
        self.default_rate = default_rate
        self.per_host = dict(per_host or {})
        # Per-SOURCE rate overrides (config/sources.yaml rate_per_host keyed by
        # source key). Precedence: per_source > per_host > default_rate.
        self.per_source = dict(per_source or {})
        self.clock = clock
        self.sleep = sleep
        self._buckets: dict[str, TokenBucket] = {}
        self._base_rates: dict[str, float] = {}
        self._penalty_until: dict[str, float] = {}
        self._sema = threading.Semaphore(max_concurrency)
        self._lock = threading.Lock()

    def _host(self, url: str) -> str:
        return (urlparse(url).hostname or url).lower()

    def _bucket_key(self, host: str, source: str | None) -> str:
        """Bucket key for a fetch. A source with its own rate override gets a
        private bucket (``source@host``) so two sources sharing a host keep
        independent rates instead of whichever created the bucket first."""
        if source and source in self.per_source:
            return f"{source}@{host}"
        return host

    def _rate_for(self, host: str, source: str | None) -> float:
        if source and source in self.per_source:
            return self.per_source[source]
        return self.per_host.get(host, self.default_rate)

    def _bucket(self, url: str, source: str | None = None) -> tuple[str, TokenBucket]:
        host = self._host(url)
        key = self._bucket_key(host, source)
        with self._lock:
            until = self._penalty_until.get(key)
            if until is not None and self.clock() >= until:
                self._penalty_until.pop(key, None)
                if key in self._buckets:
                    self._buckets[key].rate_per_sec = self._base_rates[key]
            if key not in self._buckets:
                self._base_rates[key] = self._rate_for(host, source)
                self._buckets[key] = TokenBucket(
                    rate_per_sec=self._base_rates[key], clock=self.clock, sleep=self.sleep
                )
            return key, self._buckets[key]

    def wait(self, url: str, source: str | None = None) -> float:
        _, bucket = self._bucket(url, source)
        return bucket.acquire()

    def penalize(self, url: str, seconds: float, source: str | None = None) -> None:
        key, bucket = self._bucket(url, source)
        with self._lock:
            base = self._base_rates[key]
            bucket.rate_per_sec = base / 2.0
            self._penalty_until[key] = self.clock() + seconds

    @contextmanager
    def slot(self) -> Iterator[None]:
        self._sema.acquire()
        try:
            yield
        finally:
            self._sema.release()
