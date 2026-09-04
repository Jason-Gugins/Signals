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
        # Per-SOURCE rate claims (config/sources.yaml rate_per_host keyed by
        # source key). Buckets stay keyed by HOST — two sources sharing a host
        # share one budget at the MIN of their claims, so overriding sec_edgar
        # to 8 req/s cannot stack with sec_formd's 8 into a 16 req/s burst
        # against a host ceiling of 10.
        self.per_source = dict(per_source or {})
        self.clock = clock
        self.sleep = sleep
        self._buckets: dict[str, TokenBucket] = {}
        self._base_rates: dict[str, float] = {}
        self._penalty_until: dict[str, float] = {}
        self._host_claims: dict[str, set[float]] = {}
        self._sema = threading.Semaphore(max_concurrency)
        self._lock = threading.Lock()

    def _host(self, url: str) -> str:
        return (urlparse(url).hostname or url).lower()

    def _claim(self, host: str, source: str | None) -> None:
        if source and source in self.per_source:
            self._host_claims.setdefault(host, set()).add(float(self.per_source[source]))
        else:
            # Un-overridden sources claim the default, keeping a shared host
            # at the most conservative rate anyone asked for.
            self._host_claims.setdefault(host, set()).add(float(self.default_rate))

    def _effective_rate(self, host: str) -> float:
        claims = set(self._host_claims.get(host, ()))
        if host in self.per_host:
            claims.add(float(self.per_host[host]))
        if not claims:
            return self.default_rate
        return min(claims)

    def _bucket(self, url: str, source: str | None = None) -> tuple[str, TokenBucket]:
        host = self._host(url)
        with self._lock:
            self._claim(host, source)
            rate = self._effective_rate(host)
            until = self._penalty_until.get(host)
            if until is not None and self.clock() >= until:
                self._penalty_until.pop(host, None)
                if host in self._buckets:
                    self._buckets[host].rate_per_sec = rate
            if host not in self._buckets:
                self._base_rates[host] = rate
                self._buckets[host] = TokenBucket(
                    rate_per_sec=rate, clock=self.clock, sleep=self.sleep
                )
            elif rate != self._base_rates[host]:
                # A later claim changed the shared-host rate; honor it unless
                # the bucket is mid-penalty (restore happens at expiry).
                self._base_rates[host] = rate
                if host not in self._penalty_until:
                    self._buckets[host].rate_per_sec = rate
            return host, self._buckets[host]

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
