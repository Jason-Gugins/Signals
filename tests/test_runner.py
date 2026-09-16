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


# ── per-source empty-log backoff keys ───────────────────────────────────────


def _fake_backoff_runner(log):
    from types import SimpleNamespace

    return SimpleNamespace(empty_log=log)


def _mk_task(slug):
    from types import SimpleNamespace

    return SimpleNamespace(meta={"product_slug": slug})


def test_filter_backoff_derives_source_per_adapter(tmp_path):
    """marketplace_trustradius slugs are keyed under 'trustradius:<slug>'."""
    from types import SimpleNamespace

    from src.pipeline.runner import CollectorRunner
    from src.sources.marketplace.empty_log import EmptyLog

    log = EmptyLog(tmp_path / "e.json", clock=lambda: 1000.0)
    for _ in range(3):
        log.record_empty("19319/JIRA", "trustradius")
    adapter = SimpleNamespace(key="marketplace_trustradius")
    task = _mk_task("19319/JIRA")
    out = CollectorRunner._filter_backoff(
        _fake_backoff_runner(log), adapter, [task]
    )
    assert out == []  # in backoff under its own source key
    # a g2-keyed entry must NOT trip the trustradius adapter
    log_g2 = EmptyLog(tmp_path / "g.json", clock=lambda: 1000.0)
    for _ in range(3):
        log_g2.record_empty("19319/JIRA", "g2")
    out2 = CollectorRunner._filter_backoff(
        _fake_backoff_runner(log_g2), adapter, [task]
    )
    assert out2 == [task]


# ── C4: the Workday job-DETAIL pass must not inflate the hiring-trend count ─


_LIST_JOBS = 6


class DetailPassAdapter(OkAdapter):
    """Workday-style board: the LIST pass harvests every posting, then a
    capped DETAIL pass re-describes the SAME postings (same ``external_id``,
    richer ``description``) — exactly what commit 1a38195 introduced."""

    key = "ats_workday"
    requires = ()

    def harvest_jobs(self, doc, account, task_meta):
        from src.sources.ats.common import JobPost

        detail = doc.url.endswith("/detail")
        return [
            JobPost(
                external_id=f"j{i}",
                title=f"Role {i}",
                url=f"https://x/{i}",
                posted_at="2026-08-01",
                description=("full detail body" if detail else None),
            )
            for i in range(_LIST_JOBS)
        ]

    def follow_tasks(self, doc, account, task_meta):
        if doc.url.endswith("/detail"):
            return []
        return [
            FetchTask(
                source=self.key,
                url="https://ok.test/acme.com/detail",
                domain=account.domain,
            )
        ]

    def parse(self, doc, account, task_meta):
        return []


def test_detail_pass_does_not_inflate_hiring_trend(tmp_path, monkeypatch):
    """A posting harvested from TWO docs (list + detail) counts ONCE.

    Pre-fix the accumulators were plain lists, so the detail pass doubled the
    per-domain count (6 -> 12) and, against a 6-role baseline, fired a FALSE
    ``hiring_surge`` (min_delta_pct 25 / min_count 5)."""
    import src.sources.jobsignals.trend as trend_mod

    saved: dict = {}
    monkeypatch.setattr(
        trend_mod, "load_stats", lambda *a, **k: {"acme.com": {"count": _LIST_JOBS}}
    )
    monkeypatch.setattr(
        trend_mod, "save_stats", lambda stats, *a, **k: saved.update(stats)
    )

    acct = Account(domain="acme.com", ats_vendor="workday", ats_token="boardtok")
    by_url = {
        "https://ok.test/acme.com": b"list",
        "https://ok.test/acme.com/detail": b"detail",
    }
    _, _, db = _harness(tmp_path, [acct], [DetailPassAdapter()], by_url, max_passes=2)

    # The detail pass re-describes postings the list pass already harvested:
    # the trend count must stay at the LIST-ONLY count.
    assert saved.get("acme.com", {}).get("count") == _LIST_JOBS
    snap = db.one("SELECT open_count FROM job_snapshots WHERE domain='acme.com'")
    assert snap is not None and snap["open_count"] == _LIST_JOBS
    # ...and the duplicated count must not fake a surge off a real baseline.
    types = {r["signal_type"] for r in db.query("SELECT signal_type FROM signals")}
    assert "hiring_surge" not in types


def test_filter_backoff_still_keys_g2_under_g2(tmp_path):
    from types import SimpleNamespace

    from src.pipeline.runner import CollectorRunner
    from src.sources.marketplace.empty_log import EmptyLog

    log = EmptyLog(tmp_path / "e.json", clock=lambda: 1000.0)
    for _ in range(3):
        log.record_empty("sierra", "g2")
    adapter = SimpleNamespace(key="marketplace_g2")
    task = _mk_task("sierra")
    out = CollectorRunner._filter_backoff(
        _fake_backoff_runner(log), adapter, [task]
    )
    assert out == []
    assert log.entry("sierra", "g2").get("cycles") == 3


# ── F8: generic detail-rotation hooks ───────────────────────────────────────
#
# Measured on a live run (83 postings, 10 described): details are emitted per
# LIST page, so with the default 2-wave pass budget only page 0's details ever
# executed; and the adapter's slice was POSITIONAL, so the same 10 postings won
# every cycle and the other 73 could never be reached. The runner now offers
# three GENERIC, opt-in hooks (all gated on class attributes, so no other
# adapter changes behaviour):
#   follow_passes     -> raise max_passes (page 1..N's details need a 3rd wave)
#   detail_selection  -> inject the described-set so the adapter can rotate
#   detail_budget     -> cap the detail-marked fetches per (adapter, account)
# The board below speaks the Workday shape (a list page emits one pagination
# task per remaining page plus one detail task per UNDESCRIBED posting) but is
# driven purely by URLs/meta, so no fixture parsing is involved.

_BOARD_PAGE = 10  # postings per page in the fake board


def _board_list_url(domain: str, offset: int) -> str:
    return f"https://board.test/{domain}/jobs?offset={offset}"


def _board_detail_url(domain: str, page: int, i: int) -> str:
    return f"https://board.test/{domain}/job?page={page}&i={i}"


class RotationBoardAdapter(OkAdapter):
    """Workday-shaped board: paginated list pages + per-page detail tasks."""

    key = "ats_workday"
    requires = ()
    follow_passes = 3
    detail_selection = True
    detail_budget = 10

    def __init__(self, pages: int = 3, budget: int | None = None, page_size: int = _BOARD_PAGE):
        self.pages = pages
        self.page_size = page_size
        self.total = pages * page_size
        self.meta_seen: list[dict] = []
        if budget is not None:
            self.detail_budget = budget

    def _posting_id(self, page: int, i: int) -> str:
        return f"/job/P{page}-{i}"

    def plan(self, account, cursor):
        return [
            FetchTask(
                source=self.key,
                url=_board_list_url(account.domain, 0),
                domain=account.domain,
                method="POST",
            )
        ]

    def follow_tasks(self, doc, account, task_meta):
        self.meta_seen.append(dict(task_meta or {}))
        if "/job?" in doc.url:
            return []  # a detail payload carries no page: never re-fetch
        page = int(doc.url.split("offset=")[1]) // self.page_size
        described = {
            str(x) for x in ((task_meta or {}).get("described_external_ids") or ())
        }
        out: list[FetchTask] = []
        # Pagination FIRST, exactly like WorkdaySource.
        if page == 0:
            for off in range(self.page_size, self.total, self.page_size):
                out.append(
                    FetchTask(
                        source=self.key,
                        url=_board_list_url(account.domain, off),
                        domain=account.domain,
                        method="POST",
                    )
                )
        for i in range(self.page_size):
            ext = self._posting_id(page, i)
            if ext in described:
                continue
            out.append(
                FetchTask(
                    source=self.key,
                    url=_board_detail_url(account.domain, page, i),
                    domain=account.domain,
                    meta={"detail": True, "external_id": ext},
                )
            )
        return out

    def harvest_jobs(self, doc, account, task_meta):
        from src.sources.ats.common import JobPost

        if "/job?" in doc.url:
            ext = (task_meta or {}).get("external_id") or ""
            return [
                JobPost(
                    external_id=ext,
                    title=ext,
                    url=doc.url,
                    posted_at="2026-08-01",
                    description=f"full body of {ext}",
                )
            ]
        page = int(doc.url.split("offset=")[1]) // self.page_size
        return [
            JobPost(
                external_id=self._posting_id(page, i),
                title=self._posting_id(page, i),
                url=doc.url,
                posted_at="2026-08-01",
            )
            for i in range(self.page_size)
        ]

    def parse(self, doc, account, task_meta):
        return []


class PlainBoardAdapter(RotationBoardAdapter):
    """The same board WITHOUT any of the opt-ins: the shape every other
    source adapter has. Its three hooks are explicitly non-opt-in, so it is
    also the guard against a truthiness bug in the runner's gating."""

    key = "ats_greenhouse"
    requires = ()
    follow_passes = None
    detail_selection = False
    detail_budget = None


def _board_account(key: str) -> Account:
    return Account(
        domain="acme.com",
        ats_vendor=key[len("ats_"):],
        ats_token="boardtok",
    )


def _silence_jobsignals_stats(monkeypatch) -> None:
    """The shared hiring-trend state file must never be written by a test
    (same pattern as the C4 test above)."""
    import src.sources.jobsignals.trend as trend_mod

    monkeypatch.setattr(trend_mod, "load_stats", lambda *a, **k: {})
    monkeypatch.setattr(trend_mod, "save_stats", lambda stats, *a, **k: None)


def _detail_pairs(runner) -> list[tuple[int, int]]:
    """(page, index) of every DETAIL fetch this cycle made, in request order."""
    out: list[tuple[int, int]] = []
    for url in runner.fetcher.calls:
        if "/job?" not in url:
            continue
        params = dict(
            kv.split("=", 1) for kv in url.split("?", 1)[1].split("&") if "=" in kv
        )
        out.append((int(params["page"]), int(params["i"])))
    return out


def test_detail_budget_caps_fetches_per_cycle_and_logs_the_drop(tmp_path, monkeypatch):
    """Budget: 30 postings, budget 10 -> EXACTLY 10 detail fetches in one cycle,
    and the surplus is logged (never dropped silently)."""
    from loguru import logger

    _silence_jobsignals_stats(monkeypatch)
    adapter = RotationBoardAdapter(pages=3, budget=10)
    messages: list[str] = []
    sink = logger.add(messages.append, level="INFO", format="{message}")
    try:
        runner, stats, _ = _harness(
            tmp_path, [_board_account("ats_workday")], [adapter], {},
            max_passes=2, max_workers=1, force=True,
        )
    finally:
        logger.remove(sink)
    assert stats.failed == 0
    details = _detail_pairs(runner)
    assert len(details) == 10
    # the first wave of detail tasks is page 0's, in page order
    assert details == [(0, i) for i in range(10)]
    # pages 1 and 2's 20 detail tasks were queued and dropped -> logged once
    dropped = [m for m in messages if "dropped" in str(m) and "detail" in str(m)]
    assert dropped, messages
    assert any("20" in m for m in dropped)


def test_detail_rotation_converges_and_never_refetches_a_described_posting(tmp_path, monkeypatch):
    """Convergence: cycle 2 details the NEXT batch, with ZERO overlap.

    Cycle 1 gets page 0's 10 (the budget); cycle 2 must move on to page 1 and
    must not re-request a posting that now carries a description.
    """
    _silence_jobsignals_stats(monkeypatch)
    adapter = RotationBoardAdapter(pages=3, budget=10)
    acct = _board_account("ats_workday")

    runner1, stats1, db = _harness(
        tmp_path, [acct], [adapter], {}, max_passes=2, max_workers=1, force=True
    )
    cycle1 = _detail_pairs(runner1)
    assert stats1.failed == 0
    assert cycle1 == [(0, i) for i in range(10)]

    runner2, stats2, db = _harness(
        tmp_path, [acct], [adapter], {}, max_passes=2, max_workers=1, force=True
    )
    cycle2 = _detail_pairs(runner2)
    assert stats2.failed == 0
    assert cycle2 == [(1, i) for i in range(10)]
    assert set(cycle1) & set(cycle2) == set()  # zero overlap

    described = db.one(
        "SELECT COUNT(*) AS n FROM jobs WHERE description IS NOT NULL AND description <> ''"
    )["n"]
    assert described == 20


def test_detail_pass_waves_reach_later_pages(tmp_path, monkeypatch):
    """Pages 1..N's details DO execute: with follow_passes=3 the detail tasks
    emitted while fetching pagination page 1 get a third wave to run in (with
    the default 2 waves they were emitted in pass 1 and dropped)."""
    _silence_jobsignals_stats(monkeypatch)
    adapter = RotationBoardAdapter(pages=2, budget=100)
    runner, stats, _ = _harness(
        tmp_path, [_board_account("ats_workday")], [adapter], {},
        max_passes=2, max_workers=1, force=True,
    )
    assert stats.failed == 0
    pairs = _detail_pairs(runner)
    assert {(0, i) for i in range(10)} <= set(pairs)
    # a posting that only ever existed on page 1 (its externalPath is NOT on
    # page 0 because page 0's ids are /job/P0-*)
    assert (1, 0) in pairs


def test_non_optin_adapter_is_untouched(tmp_path, monkeypatch):
    """Isolation / regression guard for every other source: no meta injection,
    no extra pass, no per-cycle detail cap."""
    _silence_jobsignals_stats(monkeypatch)
    # 30 postings on ONE page: the guard is that the runner neither caps them
    # (no budget) nor hands the adapter a described-set. Pages 1..N are still
    # listed but their details never get a wave -- the pre-F8 behaviour.
    adapter = PlainBoardAdapter(pages=3, page_size=30)
    runner, stats, _ = _harness(
        tmp_path, [_board_account("ats_greenhouse")], [adapter], {},
        max_passes=2, max_workers=1, force=True,
    )
    assert stats.failed == 0
    # 1 list page + 2 pagination pages + ALL 30 page-0 details: no cap, and no
    # third pass (max_passes stayed at the caller's 2).
    assert len(runner.fetcher.calls) == 33
    assert _detail_pairs(runner) == [(0, i) for i in range(30)]
    assert all("described_external_ids" not in m for m in adapter.meta_seen)


def test_every_posting_is_described_within_three_cycles(tmp_path, monkeypatch):
    """HEADLINE: a 30-posting board with a per-cycle budget of 10 converges --
    after three cycles EVERY posting has a description, each cycle takes the
    next 10, and no posting is detailed twice."""
    _silence_jobsignals_stats(monkeypatch)
    adapter = RotationBoardAdapter(pages=3, budget=10)
    acct = _board_account("ats_workday")

    per_cycle: list[list[tuple[int, int]]] = []
    db = None
    for _ in range(3):
        runner, stats, db = _harness(
            tmp_path, [acct], [adapter], {}, max_passes=2, max_workers=1, force=True
        )
        assert stats.failed == 0
        per_cycle.append(_detail_pairs(runner))

    assert per_cycle == [[(page, i) for i in range(10)] for page in range(3)]
    flat = [p for cycle in per_cycle for p in cycle]
    assert len(flat) == len(set(flat)) == 30  # no posting detailed twice
    row = db.one(
        "SELECT COUNT(*) AS n FROM jobs WHERE description IS NOT NULL AND description <> ''"
    )
    assert row["n"] == 30  # every posting described
    total = db.one("SELECT COUNT(*) AS n FROM jobs")["n"]
    assert total == 30
