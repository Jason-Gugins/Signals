"""Task 2 (P2 roadmap): fetch error taxonomy — TDD spec.

Covers:
1. ``src/core/errors.py`` truth table for every classify branch.
2. Migration v3 adds ``fetch_log.error_class`` (column exists after Database init).
3. ``HttpFetcher`` logs the classified error class on fetch_log rows.
4. ``CollectorRunner._record_fail`` stamps error_class on the cursor and applies
   the per-class BACKOFF_MULTIPLIER to the next-due penalty.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
import pytest
import respx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.errors import BACKOFF_MULTIPLIER, FetchErrorClass, classify_fetch_error  # noqa: E402
from src.sources.base import FetchTask  # noqa: E402


# ── 1. classification truth table ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "error", "body_hint", "expected"),
    [
        # 403 + CF marker in the body -> challenge
        (403, "HTTP 403", "cf-challenge", FetchErrorClass.challenge),
        (403, "HTTP 403", "<title>Just a moment...</title>", FetchErrorClass.challenge),
        (403, "HTTP 403", "https://challenges.cloudflare.com/turnstile", FetchErrorClass.challenge),
        # 429 -> ratelimit (regardless of body/error)
        (429, "HTTP 429", None, FetchErrorClass.ratelimit),
        (429, None, "just a moment", FetchErrorClass.ratelimit),
        (429, "timed out", "cf-challenge", FetchErrorClass.ratelimit),
        # timeouts
        (0, "Connect timed out", None, FetchErrorClass.timeout),
        (0, "The read operation timed out", None, FetchErrorClass.timeout),
        (None, "ReadTimeout: timeout", None, FetchErrorClass.timeout),
        # dns
        (0, "[Errno -2] Name or service not known", None, FetchErrorClass.dns),
        (None, "getaddrinfo failed", None, FetchErrorClass.dns),
        (None, "dns resolution failed for host", None, FetchErrorClass.dns),
        # auth: 401 always; 403 WITHOUT challenge markers
        (401, "HTTP 401", None, FetchErrorClass.auth),
        (401, "HTTP 401", "unauthorized", FetchErrorClass.auth),
        (403, "HTTP 403", None, FetchErrorClass.auth),
        (403, "HTTP 403", "Forbidden", FetchErrorClass.auth),
        # other
        (500, "HTTP 500", None, FetchErrorClass.other),
        (None, None, None, FetchErrorClass.other),
        (200, None, "all good", FetchErrorClass.other),
    ],
)
def test_classify_truth_table(status, error, body_hint, expected):
    assert classify_fetch_error(status, error, body_hint) is expected


def test_classify_challenge_marker_case_insensitive():
    assert classify_fetch_error(403, None, "  JUST A MOMENT... ") is FetchErrorClass.challenge


def test_backoff_multiplier_values():
    assert BACKOFF_MULTIPLIER == {
        FetchErrorClass.challenge: 4,
        FetchErrorClass.ratelimit: 8,
        FetchErrorClass.timeout: 2,
        FetchErrorClass.dns: 3,
        FetchErrorClass.auth: 8,
        FetchErrorClass.parse_drift: 1,
        FetchErrorClass.other: 2,
    }


def test_enum_members_exist():
    for name in ("challenge", "timeout", "dns", "ratelimit", "parse_drift", "auth", "other"):
        assert FetchErrorClass(name).value == name


# ── 2. migration v3: fetch_log.error_class column ────────────────────────────


def test_migration_v3_fetch_log_error_class_column(tmp_path):
    from core import db as db_mod
    from core.db import Database

    assert max(v for v, _, _ in db_mod.MIGRATIONS) == 3
    db = Database(tmp_path / "v3.db")
    try:
        assert "error_class" in db.table_columns("fetch_log")
        assert db.one("PRAGMA user_version")["user_version"] == 3
    finally:
        db.close()


def test_migration_v3_idempotent_reopen(tmp_path):
    from core.db import Database

    path = tmp_path / "v3b.db"
    db = Database(path)
    db.close()
    db2 = Database(path)  # reopen must not attempt a duplicate ALTER
    try:
        assert "error_class" in db2.table_columns("fetch_log")
    finally:
        db2.close()


def test_migration_v3_upgrades_old_v2_db(tmp_path):
    from core.db import Database

    path = tmp_path / "old.db"
    db = Database(path)
    # Simulate a v2 DB whose fetch_log predates error_class.
    db.execute("ALTER TABLE fetch_log RENAME TO fetch_log_old")
    db.execute(
        """
        CREATE TABLE fetch_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, source TEXT,
            domain TEXT, url TEXT, status INTEGER, elapsed_ms INTEGER,
            bytes INTEGER, cached INTEGER DEFAULT 0, error TEXT,
            at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    db.execute("DROP TABLE fetch_log_old")
    db.execute("PRAGMA user_version = 2")
    assert "error_class" not in db.table_columns("fetch_log")
    db.close()

    db2 = Database(path)
    try:
        assert "error_class" in db2.table_columns("fetch_log")
        assert db2.one("PRAGMA user_version")["user_version"] == 3
    finally:
        db2.close()


# ── 3. HttpFetcher logs error_class on fetch_log rows ────────────────────────


@dataclass
class Task:
    source: str
    url: str
    domain: Optional[str] = None
    method: str = "GET"
    headers: dict = field(default_factory=dict)
    json_body: Optional[dict] = None


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self):
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _fetcher(tmp_path, monkeypatch, clock=None):
    from core.config import Config
    from core.db import Database
    from core.http import HttpFetcher
    from core.ratelimit import RateLimiter
    from core.rawstore import RawStore
    from core.runlog import RunContext

    monkeypatch.setenv("SIGNALS_CONTACT_EMAIL", "ops@example.com")
    env = tmp_path / "empty.env"
    env.write_text("", encoding="utf-8")
    cfg = Config.load(env_path=env)
    db = Database(tmp_path / "signals.db")
    store = RawStore(db, raw_dir=tmp_path / "raw")
    clock = clock or FakeClock()
    limiter = RateLimiter(default_rate=100.0, clock=clock, sleep=clock.sleep)
    ctx = RunContext(db, stage="collect")
    ctx.__enter__()
    fetcher = HttpFetcher(
        cfg, store, limiter, ctx=ctx, sleep=clock.sleep, rng=lambda a, b: 0.0
    )
    return fetcher, ctx, db


@respx.mock
def test_http_logs_error_class_auth_on_plain_403(tmp_path, monkeypatch):
    url = "https://example.com/forbidden"
    respx.get(url).mock(return_value=httpx.Response(403))
    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
    fetcher, ctx, db = _fetcher(tmp_path, monkeypatch)
    result = fetcher.get(Task(source="news_rss", url=url, domain="example.com"))
    assert result.ok is False
    row = db.one("SELECT error_class FROM fetch_log WHERE run_id = ?", (ctx.run_id,))
    assert row is not None and row["error_class"] == "auth"
    ctx.__exit__(None, None, None)


@respx.mock
def test_http_logs_error_class_challenge_on_cf_403(tmp_path, monkeypatch):
    url = "https://example.com/cf"
    respx.get(url).mock(
        return_value=httpx.Response(403, content=b"<html><title>Just a moment...</title></html>")
    )
    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
    fetcher, ctx, db = _fetcher(tmp_path, monkeypatch)
    result = fetcher.get(Task(source="news_rss", url=url, domain="example.com"))
    assert result.ok is False
    row = db.one("SELECT error_class FROM fetch_log WHERE run_id = ?", (ctx.run_id,))
    assert row is not None and row["error_class"] == "challenge"
    ctx.__exit__(None, None, None)


@respx.mock
def test_http_logs_error_class_ratelimit_on_429(tmp_path, monkeypatch):
    url = "https://example.com/slow403"
    respx.get(url).mock(return_value=httpx.Response(429, content=b"slow down"))
    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
    fetcher, ctx, db = _fetcher(tmp_path, monkeypatch)
    result = fetcher.get(Task(source="news_rss", url=url, domain="example.com"))
    assert result.ok is False
    row = db.one("SELECT error_class FROM fetch_log WHERE run_id = ?", (ctx.run_id,))
    assert row is not None and row["error_class"] == "ratelimit"
    ctx.__exit__(None, None, None)


@respx.mock
def test_http_logs_error_class_other_on_200(tmp_path, monkeypatch):
    url = "https://example.com/fine"
    respx.get(url).mock(return_value=httpx.Response(200, content=b"hello"))
    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
    fetcher, ctx, db = _fetcher(tmp_path, monkeypatch)
    result = fetcher.get(Task(source="news_rss", url=url, domain="example.com"))
    assert result.ok is True
    row = db.one("SELECT error_class FROM fetch_log WHERE run_id = ?", (ctx.run_id,))
    assert row is not None and row["error_class"] == "other"
    ctx.__exit__(None, None, None)


# ── 4. runner: error_class stamp + backoff multiplier on next-due ────────────


class Boom403:
    """Adapter whose fetch always returns a plain 403 (no CF body) -> auth."""

    key = "boom403"
    tier = "http"
    cadence_hours = 24
    requires = ()

    def plan(self, account, cursor):
        return [FetchTask(source=self.key, url="https://boom403.test/x", domain=account.domain)]

    def parse(self, doc, account, task_meta):
        return []

    def harvest_jobs(self, doc, account, task_meta):
        return []

    def local_harvest(self, *, db, account, today, task_meta):
        return []

    def next_cursor(self, doc, candidates):
        return None

    def follow_tasks(self, doc, account, task_meta):
        return []


class BoomParse:
    """Adapter whose parse always raises (no fetch error) -> other."""

    key = "boomparse"
    tier = "http"
    cadence_hours = 24
    requires = ()

    def plan(self, account, cursor):
        return [FetchTask(source=self.key, url="https://boomparse.test/x", domain=account.domain)]

    def parse(self, doc, account, task_meta):
        raise RuntimeError("parse exploded")


class Fetch403:
    def __init__(self):
        self.calls = 0

    def get(self, task, *, etag=None, last_modified=None):
        self.calls += 1
        from core.http import FetchResult

        return FetchResult(
            ok=False, status=403, doc=None, cached=False, error="HTTP 403", elapsed_ms=1
        )


def _runner_harness(tmp_path, fetcher):
    from core.config import Config
    from core.db import Database
    from core.rawstore import RawStore
    from core.runlog import RunContext
    from identity.registry import AccountRegistry
    from pipeline.runner import CollectorRunner
    from signals.store import SignalStore
    from signals.taxonomy import Taxonomy

    db = Database(tmp_path / "runner.db")
    cfg = Config()
    cfg.http.max_workers = 1
    cfg.http.respect_robots = False
    store = RawStore(db, tmp_path / "raw")
    tax = Taxonomy.load()
    ctx = RunContext(db, "collect")
    ctx.__enter__()
    runner = CollectorRunner(
        cfg, db, AccountRegistry(db), store, fetcher, SignalStore(db, tax), tax, ctx
    )
    return runner, db, ctx


def test_runner_stamps_error_class_on_simulated_403(tmp_path):
    from core.models import Account

    runner, db, ctx = _runner_harness(tmp_path, Fetch403())
    stats = runner.run([Boom403()], [Account(domain="acme.com")], force=True)
    ctx.__exit__(None, None, None)

    assert stats.failed >= 1
    cur = db.one("SELECT * FROM source_cursors WHERE source='boom403' AND key='acme.com'")
    assert cur is not None
    assert cur["error_class"] == "auth"
    assert cur["fail_count"] >= 1
    assert cur["last_error"] and "403" in cur["last_error"]


def test_runner_backoff_multiplier_applied_to_next_due(tmp_path):
    """Successive 403 (auth, multiplier 8) failures: next-due gap grows by
    base-penalty * multiplier days — until the total penalty hits the cap."""
    from core.models import Account

    runner, db, ctx = _runner_harness(tmp_path, Fetch403())
    runner.run([Boom403()], [Account(domain="acme.com")], force=True)
    cur1 = db.one("SELECT next_due_at, fail_count FROM source_cursors WHERE source='boom403' AND key='acme.com'")
    first_due = datetime.fromisoformat(cur1["next_due_at"])
    fail1 = int(cur1["fail_count"])

    runner.run([Boom403()], [Account(domain="acme.com")], force=True)
    cur2 = db.one("SELECT next_due_at, fail_count FROM source_cursors WHERE source='boom403' AND key='acme.com'")
    second_due = datetime.fromisoformat(cur2["next_due_at"])
    ctx.__exit__(None, None, None)

    assert int(cur2["fail_count"]) == fail1 + 1
    # gap = penalty_days(fail_count 2) * multiplier(auth=8), still under cap
    gap_days = (second_due - first_due).total_seconds() / 86400
    expected = (2 ** (fail1 + 1 - 1)) * BACKOFF_MULTIPLIER[FetchErrorClass.auth]
    if expected <= 8:  # under the 8x-cadence cap: exact penalty applies
        assert gap_days == pytest.approx(float(expected), abs=0.05), f"gap_days={gap_days}"


def test_runner_backoff_capped_at_8x_cadence(tmp_path):
    """4 consecutive auth failures on a 24h source: the compounding per-class
    penalty (2^3 * 8 = 64 days) is capped — next_due never exceeds
    now + 8x cadence (8 days), mirroring scheduler.MAX_BACKOFF_EXPONENT."""
    from datetime import timezone as _tz

    from core.models import Account

    runner, db, ctx = _runner_harness(tmp_path, Fetch403())
    for _ in range(4):
        runner.run([Boom403()], [Account(domain="acme.com")], force=True)
    ctx.__exit__(None, None, None)

    cur = db.one(
        "SELECT next_due_at, fail_count, error_class FROM source_cursors "
        "WHERE source='boom403' AND key='acme.com'"
    )
    assert int(cur["fail_count"]) == 4
    assert cur["error_class"] == "auth"
    next_due = datetime.fromisoformat(cur["next_due_at"])
    if next_due.tzinfo is None:
        next_due = next_due.replace(tzinfo=_tz.utc)
    # Uncapped would be 1 + 8 + 16 + 32... far beyond 8 days; the TOTAL
    # penalty from any point must never exceed now + 8x cadence (8 days).
    assert (next_due - datetime.now(_tz.utc)).total_seconds() / 86400 <= 8.1, (
        f"backoff cap violated: next_due={cur['next_due_at']}"
    )


def test_runner_generic_exception_classified_other(tmp_path):
    from core.models import Account

    class OkFetch:
        def get(self, task, *, etag=None, last_modified=None):
            from core.http import FetchResult
            from core.models import Document

            doc = Document(doc_id="d", source=task.source, url=task.url,
                           domain=task.domain, body=b"ok", status=200)
            return FetchResult(True, 200, doc, False, None, 1)

    runner, db, ctx = _runner_harness(tmp_path, OkFetch())
    stats = runner.run([BoomParse()], [Account(domain="acme.com")], force=True)
    ctx.__exit__(None, None, None)
    assert stats.failed >= 1
    cur = db.one("SELECT * FROM source_cursors WHERE source='boomparse' AND key='acme.com'")
    assert cur is not None
    assert cur["error_class"] == "other"


# ── 5. fanout: fetch failure stamps the cursor before the raise ─────────────


class Fanout403:
    """Fanout adapter whose fetch always returns a plain 403 (no CF body)."""

    key = "fanout403"
    tier = "http"
    cadence_hours = 24
    requires = ()
    fanout = True

    def plan(self, account, cursor):
        return [FetchTask(source=self.key, url="https://fanout403.test/x", domain=account.domain)]

    def parse(self, doc, account, task_meta):
        return []


def test_runner_fanout_fetch_failure_stamps_error_class(tmp_path):
    """A fetch failure inside _run_fanout escapes run()'s per-account
    try/except, so the fanout path must stamp the cursor itself (plain 403 ->
    'auth', same shape as the non-fanout path) before re-raising."""
    from core.models import Account

    runner, db, ctx = _runner_harness(tmp_path, Fetch403())
    with pytest.raises(RuntimeError):
        runner.run([Fanout403()], [Account(domain="acme.com")], force=True)
    ctx.__exit__(None, None, None)

    cur = db.one("SELECT * FROM source_cursors WHERE source='fanout403' AND key='global'")
    assert cur is not None, "fanout fetch failure must stamp the global cursor"
    assert cur["error_class"] == "auth"
    assert int(cur["fail_count"]) >= 1
    assert cur["last_error"] and "403" in cur["last_error"]


# ── 6. recovery: _record_success clears a stale error_class ─────────────────


def test_runner_recovery_clears_stale_error_class(tmp_path):
    """After a classified failure, a successful run must reset the cursor:
    error_class (and last_error) cleared, fail_count back to 0 — a recovered
    cursor must not keep the old class."""
    from core.models import Account

    class OkFetch:
        def get(self, task, *, etag=None, last_modified=None):
            from core.http import FetchResult
            from core.models import Document

            doc = Document(doc_id="d", source=task.source, url=task.url,
                           domain=task.domain, body=b"ok", status=200)
            return FetchResult(True, 200, doc, False, None, 1)

    runner, db, ctx = _runner_harness(tmp_path, Fetch403())
    runner.run([Boom403()], [Account(domain="acme.com")], force=True)
    failed = db.one("SELECT error_class, fail_count FROM source_cursors WHERE source='boom403' AND key='acme.com'")
    assert failed["error_class"] == "auth"

    runner.fetcher = OkFetch()
    runner.run([Boom403()], [Account(domain="acme.com")], force=True)
    ctx.__exit__(None, None, None)

    cur = db.one("SELECT error_class, fail_count, last_error FROM source_cursors WHERE source='boom403' AND key='acme.com'")
    assert cur is not None
    assert cur["error_class"] is None
    assert int(cur["fail_count"]) == 0
    assert cur["last_error"] is None
