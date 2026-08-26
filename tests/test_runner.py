"""Tests for the resilient concurrent collector runner."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.core.config import Config
from src.core.db import Database
from src.core.models import Account, Document
from src.core.rawstore import RawStore
from src.core.runlog import RunContext
from src.identity.registry import AccountRegistry
from src.pipeline.runner import CollectorRunner, RunnerStats
from src.signals.store import SignalStore
from src.signals.taxonomy import Taxonomy
from src.sources.base import FetchTask, SignalCandidate, SourceAdapter


class FakeFetch:
    def __init__(self, by_url: dict[str, object]):
        self.by_url = by_url
        self.calls: list[str] = []
        self.in_flight = 0
        self.max_seen = 0
        self._lock = threading.Lock()

    def get(self, task, *, etag=None, last_modified=None):
        from src.core.http import FetchResult

        with self._lock:
            self.in_flight += 1
            self.max_seen = max(self.max_seen, self.in_flight)
        try:
            self.calls.append(task.url)
            val = self.by_url.get(task.url)
            if val == "304":
                return FetchResult(True, 304, None, True, None, 1)
            if isinstance(val, Exception):
                raise val
            body = val if isinstance(val, (bytes, bytearray)) else b"ok"
            doc = Document(doc_id="d", source=task.source, url=task.url, domain=task.domain, body=bytes(body), status=200)
            return FetchResult(True, 200, doc, False, None, 1)
        finally:
            with self._lock:
                self.in_flight -= 1


class OkAdapter(SourceAdapter):
    key = "ok"
    tier = "http"
    cadence_hours = 24

    def plan(self, account, cursor):
        return [FetchTask(source=self.key, url=f"https://ok.test/{account.domain}", domain=account.domain)]

    def parse(self, doc, account, task_meta):
        return [
            SignalCandidate("award", "2026-08-01", f"award:{account.domain}", title="A"),
        ]


class NeedsCik(SourceAdapter):
    key = "needs_cik"
    tier = "http"
    requires = ("cik",)

    def plan(self, account, cursor):
        return [FetchTask(source=self.key, url="https://sec.test/x", domain=account.domain)]

    def parse(self, doc, account, task_meta):
        return []


class BoomAdapter(SourceAdapter):
    key = "boom"
    tier = "http"

    def plan(self, account, cursor):
        return [FetchTask(source=self.key, url="https://boom.test/x", domain=account.domain)]

    def parse(self, doc, account, task_meta):
        raise RuntimeError("parse exploded")


class TwoPass(SourceAdapter):
    key = "twopass"
    tier = "http"

    def plan(self, account, cursor):
        if cursor == "p2":
            return [FetchTask(source=self.key, url="https://two.test/doc", domain=account.domain)]
        return [FetchTask(source=self.key, url="https://two.test/index", domain=account.domain)]

    def parse(self, doc, account, task_meta):
        if doc.url.endswith("/index"):
            return []
        return [SignalCandidate("award", "2026-08-02", "two:award", title="B")]

    def next_cursor(self, doc, candidates):
        if doc.url.endswith("/index"):
            return "p2"
        return "done"


class FanoutAdapter(SourceAdapter):
    key = "fan"
    tier = "http"
    fanout = True

    def plan(self, account, cursor):
        return [FetchTask(source=self.key, url="https://fan.test/all", domain=None)]

    def parse(self, doc, account, task_meta):
        return [SignalCandidate("award", "2026-08-01", f"fan:{account.domain}", title=account.domain)]


def _harness(tmp_path, accounts, adapters, by_url, **cfg_kw):
    db = Database(tmp_path / "s.db")
    cfg = Config()
    cfg.http.max_workers = cfg_kw.get("max_workers", 2)
    cfg.http.respect_robots = False
    store = RawStore(db, tmp_path / "raw")
    tax = Taxonomy.load()
    ctx = RunContext(db, "collect")
    ctx.__enter__()
    runner = CollectorRunner(
        cfg, db, AccountRegistry(db), store, FakeFetch(by_url), SignalStore(db, tax), tax, ctx
    )
    stats = runner.run(adapters, accounts, **{k: v for k, v in cfg_kw.items() if k != "max_workers"})
    ctx.__exit__(None, None, None)
    return runner, stats, db


def test_cadence_skip_and_force(tmp_path):
    acct = Account(domain="acme.com", name="Acme")
    future = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
    db = Database(tmp_path / "s.db")
    db.upsert("source_cursors", {"source": "ok", "key": "acme.com", "next_due_at": future, "fail_count": 0}, pk=("source", "key"))
    cfg = Config()
    cfg.http.respect_robots = False
    tax = Taxonomy.load()
    ctx = RunContext(db, "collect")
    ctx.__enter__()
    fetcher = FakeFetch({"https://ok.test/acme.com": b"x"})
    runner = CollectorRunner(cfg, db, AccountRegistry(db), RawStore(db, tmp_path / "raw"), fetcher, SignalStore(db, tax), tax, ctx)
    stats = runner.run([OkAdapter()], [acct], force=False)
    assert stats.fetched == 0
    assert fetcher.calls == []
    stats2 = runner.run([OkAdapter()], [acct], force=True)
    assert stats2.fetched >= 1
    ctx.__exit__(None, None, None)


def test_requires_skipped_not_failed(tmp_path):
    _, stats, _ = _harness(tmp_path, [Account(domain="acme.com")], [NeedsCik()], {})
    assert stats.failed == 0
    assert stats.by_source.get("needs_cik", {}).get("skipped", 0) >= 1


def test_one_failure_does_not_stop_others(tmp_path):
    accounts = [Account(domain="acme.com")]
    by_url = {"https://ok.test/acme.com": b"ok", "https://boom.test/x": b"bad"}
    _, stats, db = _harness(tmp_path, accounts, [BoomAdapter(), OkAdapter()], by_url)
    assert stats.signals_new >= 1
    assert stats.failed >= 1
    row = db.one("SELECT fail_count FROM source_cursors WHERE source='boom' AND key='acme.com'")
    assert row and row["fail_count"] >= 1


def test_circuit_breaker_at_five(tmp_path):
    db = Database(tmp_path / "s.db")
    db.upsert("source_cursors", {"source": "boom", "key": "acme.com", "fail_count": 5, "last_error": "x"}, pk=("source", "key"))
    cfg = Config()
    cfg.http.respect_robots = False
    tax = Taxonomy.load()
    ctx = RunContext(db, "collect")
    ctx.__enter__()
    fetcher = FakeFetch({"https://boom.test/x": b"x"})
    runner = CollectorRunner(cfg, db, AccountRegistry(db), RawStore(db, tmp_path / "raw"), fetcher, SignalStore(db, tax), tax, ctx)
    stats = runner.run([BoomAdapter()], [Account(domain="acme.com")])
    assert fetcher.calls == []
    assert stats.by_source["boom"]["skipped"] >= 1
    ctx.__exit__(None, None, None)


def test_cursor_advances_only_on_success_and_304_skips_parse(tmp_path):
    acct = Account(domain="acme.com")
    parse_hits = {"n": 0}

    class CountParse(OkAdapter):
        def parse(self, doc, account, task_meta):
            parse_hits["n"] += 1
            return super().parse(doc, account, task_meta)

    by_url = {"https://ok.test/acme.com": b"ok"}
    runner, stats, db = _harness(tmp_path, [acct], [CountParse()], by_url)
    row = db.one("SELECT cursor, fail_count FROM source_cursors WHERE source='ok' AND key='acme.com'")
    assert row["fail_count"] == 0
    assert row["cursor"]
    n1 = parse_hits["n"]
    runner.fetcher.by_url["https://ok.test/acme.com"] = "304"
    # force so cadence does not skip
    runner.run([CountParse()], [acct], force=True)
    assert parse_hits["n"] == n1


def test_two_pass_and_fanout_and_dry_run(tmp_path):
    accts = [Account(domain="acme.com"), Account(domain="beta.com")]
    by_url = {"https://two.test/index": b"i", "https://two.test/doc": b"d", "https://fan.test/all": b"f"}
    _, stats, db = _harness(tmp_path, accts, [TwoPass(), FanoutAdapter()], by_url, max_passes=2)
    assert db.one("SELECT COUNT(*) AS n FROM signals")["n"] >= 3  # two fan + one two-pass
    runner, stats2, _ = _harness(tmp_path / "d", accts, [OkAdapter()], {"https://ok.test/acme.com": b"x", "https://ok.test/beta.com": b"x"}, dry_run=True)
    assert stats2.fetched == 0
    assert runner.fetcher.calls == []


def test_thread_pool_respects_max_workers(tmp_path):
    accts = [Account(domain=f"c{i}.com") for i in range(8)]
    by_url = {f"https://ok.test/c{i}.com": b"x" for i in range(8)}
    runner, _, _ = _harness(tmp_path, accts, [OkAdapter()], by_url, max_workers=2)
    assert runner.fetcher.max_seen <= 2


def test_network_capture_skipped_without_browser(tmp_path):
    from src.sources.techstack.collector import TechstackSource

    html = Path("tests/fixtures/techstack/homepage.html").read_bytes()
    acct = Account(domain="acme.com", name="Acme")
    kinds: list[str | None] = []
    fetch = FakeFetch({"https://acme.com/": html})
    orig = fetch.get

    def wrapped(task, *, etag=None, last_modified=None):
        kinds.append((task.meta or {}).get("kind"))
        return orig(task, etag=etag, last_modified=last_modified)

    fetch.get = wrapped
    db = Database(tmp_path / "s.db")
    cfg = Config()
    cfg.http.max_workers = 6
    cfg.http.respect_robots = False
    store = RawStore(db, tmp_path / "raw")
    tax = Taxonomy.load()
    ctx = RunContext(db, "collect")
    ctx.__enter__()
    runner = CollectorRunner(
        cfg, db, AccountRegistry(db), store, fetch, SignalStore(db, tax), tax, ctx, browser=None
    )
    stats = runner.run([TechstackSource()], [acct], force=True, max_passes=1)
    ctx.__exit__(None, None, None)
    assert kinds == ["html"]
    assert stats.fetched >= 1
    assert stats.failed == 0


def test_runner_upserts_tech_from_html(tmp_path):
    from src.sources.techstack.collector import TechstackSource

    html = Path("tests/fixtures/techstack/homepage.html").read_bytes()
    acct = Account(domain="acme.com", name="Acme")
    db = Database(tmp_path / "s.db")
    cfg = Config()
    cfg.http.max_workers = 1
    cfg.http.respect_robots = False
    store = RawStore(db, tmp_path / "raw")
    tax = Taxonomy.load()
    ctx = RunContext(db, "collect")
    ctx.__enter__()
    runner = CollectorRunner(
        cfg, db, AccountRegistry(db), store, FakeFetch({"https://acme.com/": html}), SignalStore(db, tax), tax, ctx, browser=None
    )
    runner.run([TechstackSource()], [acct], force=True, max_passes=1)
    ctx.__exit__(None, None, None)
    rows = db.query("SELECT vendor FROM technologies WHERE domain=?", ("acme.com",))
    assert "hubspot" in {r["vendor"] for r in rows}


def test_runner_unions_html_and_network_tech(tmp_path):
    import json

    from src.core.http import FetchResult
    from src.sources.techstack.collector import TechstackSource

    html = Path("tests/fixtures/techstack/homepage.html").read_bytes()
    net = json.dumps(
        {
            "page_url": "https://acme.com/",
            "requests": [
                {
                    "url": "https://www.googletagmanager.com/gtm.js",
                    "host": "www.googletagmanager.com",
                    "resource_type": "script",
                }
            ],
        }
    ).encode()

    class DummyBrowser:
        def fetch(self, url, *, source, domain=None, capture_network=False, wait_ms=None):
            doc = Document(
                doc_id="net",
                source=source,
                url=url,
                domain=domain,
                body=net,
                content_type="application/json",
                status=200,
            )
            return FetchResult(True, 200, doc, False, None, 1)

    acct = Account(domain="acme.com", name="Acme")
    db = Database(tmp_path / "s.db")
    cfg = Config()
    cfg.http.max_workers = 1
    cfg.http.respect_robots = False
    store = RawStore(db, tmp_path / "raw")
    tax = Taxonomy.load()
    ctx = RunContext(db, "collect")
    ctx.__enter__()
    runner = CollectorRunner(
        cfg,
        db,
        AccountRegistry(db),
        store,
        FakeFetch({"https://acme.com/": html}),
        SignalStore(db, tax),
        tax,
        ctx,
        browser=DummyBrowser(),
    )
    runner.run([TechstackSource()], [acct], force=True, max_passes=1)
    ctx.__exit__(None, None, None)
    rows = {r["vendor"]: int(r["missing_runs"] or 0) for r in db.query("SELECT vendor, missing_runs FROM technologies WHERE domain=?", ("acme.com",))}
    assert rows.get("hubspot") == 0
    assert rows.get("gtm") == 0


class HarvestAdapter(OkAdapter):
    key = "harvest"

    def harvest_jobs(self, doc, account, task_meta):
        from src.sources.ats.common import JobPost
        return [JobPost(external_id="1", title="AE", url="https://x/1", posted_at="2026-08-01")]

    def follow_tasks(self, doc, account, task_meta):
        if doc.url.endswith("/follow"):
            return []
        return [FetchTask(source=self.key, url="https://ok.test/follow", domain=account.domain)]

    def parse(self, doc, account, task_meta):
        assert "today" in task_meta
        if doc.url.endswith("/follow"):
            return [SignalCandidate("award", "2026-08-01", "follow:1", title="F")]
        return [SignalCandidate("award", "2026-08-01", "idx:1", title="I")]


def test_hooks_jobs_follow_and_today(tmp_path):
    acct = Account(domain="acme.com")
    by_url = {"https://ok.test/acme.com": b"idx", "https://ok.test/follow": b"f"}
    _, stats, db = _harness(tmp_path, [acct], [HarvestAdapter()], by_url, max_passes=2)
    assert db.one("SELECT COUNT(*) AS n FROM jobs")["n"] == 1
    types = {r["title"] for r in db.query("SELECT title FROM signals")}
    assert "F" in types and "I" in types


def test_harvest_token_closed_snapshot(tmp_path):
    from src.sources.ats.common import JobPost, job_key

    class Board(OkAdapter):
        key = "ats_greenhouse"
        requires = ()

        def harvest_jobs(self, doc, account, task_meta):
            return [JobPost(external_id="1", title="AE", url="https://x/1", posted_at="2026-08-01")]

        def parse(self, doc, account, task_meta):
            return []

    acct = Account(domain="acme.com", ats_vendor="greenhouse", ats_token="boardtok")
    _, _, db = _harness(tmp_path, [acct], [Board()], {"https://ok.test/acme.com": b"x"}, max_passes=1)
    row = db.one("SELECT job_key, closed_at FROM jobs WHERE external_id='1'")
    assert row["job_key"] == job_key("ats_greenhouse", "boardtok", "1")
    snap = db.one("SELECT open_count FROM job_snapshots WHERE domain='acme.com'")
    assert snap["open_count"] == 1


def test_runner_routes_challenge_through_bypass(tmp_path):
    """When an http-tier html task returns a Cloudflare challenge body and a
    cloudflare_bypass is wired, the runner calls bypass.attempt and uses its
    result; meta['cloudflare_unsolved'] reflects the bypass outcome."""
    from src.core.http import FetchResult
    from src.sources.techstack.collector import TechstackSource

    challenge_body = b"<html><title>Just a moment...</title></html>"
    cleared_body = (
        b"<html><script src='https://js.hs-scripts.com/x.js'></script>"
        b"<script src='https://www.googletagmanager.com/gtm.js'></script></html>"
    )

    class _ChallengeFetch:
        """Returns a challenge body (status 200, 'Just a moment...') on get()."""
        def get(self, task, *, etag=None, last_modified=None):
            doc = Document(
                doc_id="c", source=task.source, url=task.url,
                domain=task.domain, body=challenge_body, status=200,
            )
            return FetchResult(True, 200, doc, False, None, 1)

    class _StubBypass:
        """Stub CloudflareBypass that always succeeds with cleared content."""
        def __init__(self):
            self.attempts = 0
            self.last_domain = None
            self.last_url = None

        def attempt(self, *, domain, url, user_agent, proxy="direct", source="techstack", click_show_more=False, **_):
            self.attempts += 1
            self.last_domain = domain
            self.last_url = url
            from src.sources.techstack.cf_bypass import BypassOutcome
            doc = Document(
                doc_id="b", source="techstack", url=url,
                domain=domain, body=cleared_body, status=200,
            )
            result = FetchResult(True, 200, doc, False, None, 1, [])
            return BypassOutcome(True, "browser", "js", result, [])

    acct = Account(domain="acme.com", name="Acme")
    db = Database(tmp_path / "s.db")
    cfg = Config()
    cfg.http.max_workers = 1
    cfg.http.respect_robots = False
    store = RawStore(db, tmp_path / "raw")
    tax = Taxonomy.load()
    ctx = RunContext(db, "collect")
    ctx.__enter__()
    bypass = _StubBypass()
    runner = CollectorRunner(
        cfg, db, AccountRegistry(db), store, _ChallengeFetch(),
        SignalStore(db, tax), tax, ctx, browser=None, cloudflare_bypass=bypass,
    )
    runner.run([TechstackSource()], [acct], force=True, max_passes=1)
    ctx.__exit__(None, None, None)
    # bypass was invoked for the html task
    assert bypass.attempts >= 1
    assert bypass.last_domain == "acme.com"
    # the cleared body (with real vendors) was upserted, not the challenge
    rows = {r["vendor"] for r in db.query("SELECT vendor FROM technologies WHERE domain=?", ("acme.com",))}
    assert "hubspot" in rows


def test_runner_marks_cloudflare_unsolved_on_bypass_failure(tmp_path):
    """When the bypass fails, meta['cloudflare_unsolved']=True flows to the
    collector, which records only 'cloudflare' (honest hard stop)."""
    from src.core.http import FetchResult
    from src.sources.techstack.collector import TechstackSource

    challenge_body = b"<html><title>Just a moment...</title></html>"

    class _ChallengeFetch:
        def get(self, task, *, etag=None, last_modified=None):
            doc = Document(
                doc_id="c", source=task.source, url=task.url,
                domain=task.domain, body=challenge_body, status=200,
            )
            return FetchResult(True, 200, doc, False, None, 1)

    class _FailingBypass:
        def attempt(self, *, domain, url, user_agent, proxy="direct", source="techstack", click_show_more=False, **_):
            from src.sources.techstack.cf_bypass import BypassOutcome
            return BypassOutcome(False, None, "managed", None, [])

    acct = Account(domain="acme.com", name="Acme")
    db = Database(tmp_path / "s.db")
    cfg = Config()
    cfg.http.max_workers = 1
    cfg.http.respect_robots = False
    store = RawStore(db, tmp_path / "raw")
    tax = Taxonomy.load()
    ctx = RunContext(db, "collect")
    ctx.__enter__()
    bypass = _FailingBypass()
    runner = CollectorRunner(
        cfg, db, AccountRegistry(db), store, _ChallengeFetch(),
        SignalStore(db, tax), tax, ctx, browser=None, cloudflare_bypass=bypass,
    )
    runner.run([TechstackSource()], [acct], force=True, max_passes=1)
    ctx.__exit__(None, None, None)
    # honest hard stop: only 'cloudflare' recorded, no fabricated vendors
    rows = {r["vendor"] for r in db.query("SELECT vendor FROM technologies WHERE domain=?", ("acme.com",))}
    assert rows == {"cloudflare"}


def test_full_board_marks_missing_job_closed(tmp_path):
    from src.core.config import Config
    from src.core.rawstore import RawStore
    from src.core.runlog import RunContext
    from src.identity.registry import AccountRegistry
    from src.pipeline.runner import CollectorRunner
    from src.signals.store import SignalStore
    from src.signals.taxonomy import Taxonomy
    from src.sources.ats.common import JobPost, upsert_jobs

    class Board(OkAdapter):
        key = "ats_greenhouse"
        requires = ()

        def harvest_jobs(self, doc, account, task_meta):
            return [JobPost(external_id="keep", title="AE", url="https://x/1", posted_at="2026-08-01")]

        def parse(self, doc, account, task_meta):
            return []

    acct = Account(domain="acme.com", ats_vendor="greenhouse", ats_token="t")
    db = Database(tmp_path / "s.db")
    upsert_jobs(
        db,
        "acme.com",
        [JobPost(external_id="gone", title="Old", url="https://x/g", posted_at="2026-07-01")],
        "ats_greenhouse",
        now="2026-08-01T00:00:00",
        token="t",
    )
    cfg = Config()
    cfg.http.max_workers = 1
    cfg.http.respect_robots = False
    tax = Taxonomy.load()
    ctx = RunContext(db, "collect")
    ctx.__enter__()
    runner = CollectorRunner(
        cfg, db, AccountRegistry(db), RawStore(db, tmp_path / "raw"),
        FakeFetch({"https://ok.test/acme.com": b"x"}), SignalStore(db, tax), tax, ctx,
    )
    runner.run([Board()], [acct], force=True, max_passes=1)
    ctx.__exit__(None, None, None)
    gone = db.one("SELECT closed_at FROM jobs WHERE external_id='gone'")
    assert gone["closed_at"]
    keep = db.one("SELECT closed_at FROM jobs WHERE external_id='keep'")
    assert keep["closed_at"] is None
