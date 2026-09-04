"""Per-source rate_per_host wiring (P2 Task 6).

config/sources.yaml carries per-source ``rate_per_host`` values (sec_edgar 8.0,
crtsh 0.2, bbb_profile 0.2, ...) but the limiter was built with the global
default only. These tests pin:

1. RateLimiter accepts an optional per-call ``source``; per-source rates take
   precedence over per-host, then default (asserted on the limiter's computed
   sleeps with a fake clock — no wall-clock waiting).
2. The orchestrator builds its fetcher limiter with the sources.yaml overrides.
3. HttpFetcher passes the task's source into the limiter calls.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Optional

import httpx
import respx

from src.core.config import Config
from src.core.db import Database
from src.core.http import HttpFetcher
from src.core.ratelimit import RateLimiter
from src.core.rawstore import RawStore
from src.pipeline.orchestrator import Orchestrator


class FakeClock:
    def __init__(self, start: float = 0.0):
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


# --- 1. RateLimiter source overrides (unit) -------------------------------


def test_source_override_throttles_that_source_to_its_rate():
    clock = FakeClock()
    limiter = RateLimiter(default_rate=1.0, per_source={"crtsh": 0.2}, clock=clock, sleep=clock.sleep)
    url = "https://crt.sh/?q=acme.com"
    limiter.wait(url, source="crtsh")  # free: fresh bucket
    limiter.wait(url, source="crtsh")
    # 0.2 req/s -> 5s between tokens, not the 1s default
    assert clock.sleeps and abs(clock.sleeps[-1] - 5.0) < 1e-9


def test_shared_host_takes_the_min_of_all_claims():
    clock = FakeClock()
    limiter = RateLimiter(default_rate=1.0, per_source={"crtsh": 0.2}, clock=clock, sleep=clock.sleep)
    url = "https://crt.sh/?q=acme.com"
    # crtsh claims 0.2 for the shared host bucket; news_rss (no override,
    # would claim the 1.0 default) then inherits that min instead of running
    # a second, faster bucket against the same host.
    limiter.wait(url, source="crtsh")  # fresh bucket, free
    limiter.wait(url, source="news_rss")
    assert clock.sleeps and abs(clock.sleeps[-1] - 5.0) < 1e-9


def test_two_overridden_sources_sharing_a_host_do_not_sum_budgets():
    clock = FakeClock()
    # sec_edgar and sec_formd both hit data.sec.gov at a claimed 8 req/s each;
    # private per-source buckets would allow 16 req/s against the documented
    # ceiling of 10 — the shared host bucket must run at the min (8).
    limiter = RateLimiter(
        default_rate=1.0,
        per_source={"sec_edgar": 8.0, "sec_formd": 8.0, "crtsh": 0.2},
        clock=clock,
        sleep=clock.sleep,
    )
    limiter.wait("https://data.sec.gov/submissions/CIK1.json", source="sec_edgar")
    limiter.wait("https://data.sec.gov/submissions/CIK2.json", source="sec_formd")
    assert limiter._base_rates["data.sec.gov"] == 8.0
    assert len(limiter._buckets) == 1  # one shared bucket, not source@host pairs
    # a slower source on the same host drags the shared bucket down to the min
    limiter.wait("https://data.sec.gov/submissions/CIK3.json", source="crtsh")
    assert limiter._base_rates["data.sec.gov"] == 0.2
    assert limiter._buckets["data.sec.gov"].rate_per_sec == 0.2


def test_per_host_rates_still_apply_when_source_has_no_override():
    clock = FakeClock()
    limiter = RateLimiter(default_rate=1.0, per_host={"slow.example": 0.5}, clock=clock, sleep=clock.sleep)
    url = "https://slow.example/x"
    limiter.wait(url, source="news_rss")
    limiter.wait(url, source="news_rss")
    # per-host 0.5 req/s -> 2s
    assert clock.sleeps and abs(clock.sleeps[-1] - 2.0) < 1e-9


def test_wait_without_source_keeps_host_keyed_behavior():
    clock = FakeClock()
    limiter = RateLimiter(default_rate=1.0, per_host={"slow.example": 0.5}, clock=clock, sleep=clock.sleep)
    url = "https://slow.example/x"
    limiter.wait(url)
    limiter.wait(url)
    assert clock.sleeps and abs(clock.sleeps[-1] - 2.0) < 1e-9


def test_penalize_with_source_hits_the_source_bucket():
    clock = FakeClock()
    limiter = RateLimiter(default_rate=2.0, per_source={"crtsh": 0.4}, clock=clock, sleep=clock.sleep)
    url = "https://crt.sh/x"
    limiter.wait(url, source="crtsh")
    limiter.penalize(url, seconds=10.0, source="crtsh")
    limiter.wait(url, source="crtsh")
    # base 0.4 halved to 0.2 -> next token costs 5s
    assert abs(clock.sleeps[-1] - 5.0) < 1e-9
    clock.now += 10.0
    limiter.wait(url, source="crtsh")
    limiter.wait(url, source="crtsh")
    # penalty expired -> back to base 0.4 -> 2.5s
    assert abs(clock.sleeps[-1] - 2.5) < 1e-9


# --- 2. Orchestrator wiring ------------------------------------------------


def _orch(tmp_path):
    cfg = Config()
    cfg.contact_email = "recon@example.com"
    cfg.http.respect_robots = False
    cfg.storage.db_path = str(tmp_path / "s.db")
    cfg.storage.raw_dir = str(tmp_path / "raw")
    cfg.storage.briefs_dir = str(tmp_path / "briefs")
    cfg.storage.export_dir = str(tmp_path / "exports")
    cfg.config_dir = "config"
    return Orchestrator(cfg)


def test_orchestrator_limiter_carries_source_overrides(tmp_path):
    orch = _orch(tmp_path)
    fetcher = orch._http_fetcher(None)
    try:
        per_source = fetcher.limiter.per_source
    finally:
        fetcher.close()
    # config/sources.yaml overrides land in the limiter's source map
    assert per_source["sec_edgar"] == 8.0
    assert per_source["sec_formd"] == 8.0
    assert per_source["crtsh"] == 0.2
    assert per_source["bbb_profile"] == 0.2
    assert per_source["ats_greenhouse"] == 2.0
    # sources without an explicit rate_per_host must not be pinned
    assert "jobsignals" not in per_source
    assert "owned_intent" not in per_source


# --- 3. HttpFetcher passes task.source into the limiter -------------------


@dataclass
class Task:
    source: str
    url: str
    domain: Optional[str] = None
    method: str = "GET"
    headers: dict = field(default_factory=dict)
    json_body: Optional[dict] = None


class RecordingLimiter:
    """RateLimiter stand-in recording every (url, source) call."""

    def __init__(self):
        self.calls: list[tuple[str, str, object]] = []

    def wait(self, url, source=None):
        self.calls.append(("wait", url, source))
        return 0.0

    def penalize(self, url, seconds, source=None):
        self.calls.append(("penalize", url, source))

    @contextmanager
    def slot(self):
        yield


@respx.mock
def test_http_fetcher_passes_task_source_to_limiter(tmp_path, monkeypatch):
    url = "https://example.com/page"
    route = respx.get(url)
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "1"}),
        httpx.Response(200, content=b"ok", headers={"content-type": "text/plain"}),
    ]
    monkeypatch.setenv("SIGNALS_CONTACT_EMAIL", "ops@example.com")
    env = tmp_path / "empty.env"
    env.write_text("", encoding="utf-8")
    cfg = Config.load(env_path=env)
    cfg.http.respect_robots = False
    db = Database(tmp_path / "s.db")
    store = RawStore(db, raw_dir=tmp_path / "raw")
    limiter = RecordingLimiter()
    fetcher = HttpFetcher(cfg, store, limiter, ctx=None, sleep=lambda s: None, rng=lambda a, b: 0.0)
    result = fetcher.get(Task(source="crtsh", url=url))
    assert result.ok is True
    waits = [c for c in limiter.calls if c[0] == "wait"]
    assert waits and all(c[2] == "crtsh" for c in waits)
    assert ("penalize", url, "crtsh") in limiter.calls
    fetcher.close()
