"""Tests for the pure hiring-velocity trend signal (jobsignals)."""

from __future__ import annotations

from datetime import date

from src.core.models import Account, Document
from src.sources.ats.common import JobPost
from src.sources.base import FetchTask, SourceAdapter
from src.sources.jobsignals.trend import (
    hiring_trend_signal,
    load_stats,
    save_stats,
    stats_key,
)

TODAY = date(2026, 8, 31)


def test_crossing_threshold_emits_hiring_surge():
    prev = {"count": 10}
    curr = {"count": 17}  # +70%
    cand = hiring_trend_signal("acme.com", prev, curr, today=TODAY)
    assert cand is not None
    assert cand.signal_type == "hiring_surge"
    assert cand.evidence_data["delta_pct"] == 70.0
    assert cand.evidence_data["count_delta"] == 7
    assert cand.evidence_data["prev"] == {"count": 10}
    assert cand.evidence_data["current"] == {"count": 17}
    assert cand.evidence_data["thresholds"]["min_delta_pct"] == 25.0
    assert cand.evidence_data["thresholds"]["min_count"] == 5.0


def test_below_threshold_returns_none():
    prev = {"count": 10}
    curr = {"count": 12}  # +20% < 25%
    assert hiring_trend_signal("acme.com", prev, curr, today=TODAY) is None


def test_decrease_returns_none():
    prev = {"count": 10}
    curr = {"count": 5}
    assert hiring_trend_signal("acme.com", prev, curr, today=TODAY) is None


def test_first_run_prev_none_returns_none():
    curr = {"count": 40}
    assert hiring_trend_signal("acme.com", None, curr, today=TODAY) is None


def test_zero_baseline_returns_none():
    # 0 -> 3 is an infinite ratio; without a baseline the percentage is
    # meaningless, so no signal.
    assert hiring_trend_signal("acme.com", {"count": 0}, {"count": 3}, today=TODAY) is None


def test_natural_key_stable():
    prev = {"count": 10}
    curr = {"count": 20}
    c1 = hiring_trend_signal("acme.com", prev, curr, today=TODAY)
    c2 = hiring_trend_signal("acme.com", prev, curr, today="2026-08-31")
    assert c1 is not None and c2 is not None
    assert c1.natural_key == c2.natural_key == "hrtrend:acme.com:2026-08-31"
    # idempotent: same inputs -> same natural key (upsert dedupes)
    assert c1.natural_key == hiring_trend_signal(
        "acme.com", prev, curr, today=date(2026, 8, 31)
    ).natural_key


def test_custom_threshold():
    prev = {"count": 10}
    curr = {"count": 12}
    cand = hiring_trend_signal("acme.com", prev, curr, today=TODAY, min_delta_pct=15.0, min_count=0)
    assert cand is not None
    assert cand.evidence_data["delta_pct"] == 20.0
    # and the default thresholds reject the same delta (+2 is below the floor)
    assert hiring_trend_signal("acme.com", prev, curr, today=TODAY) is None


def test_min_count_floor_blocks_small_boards():
    # 3 -> 4 is +33% (>= 25%) but below the absolute-count floor: noise.
    assert hiring_trend_signal("acme.com", {"count": 3}, {"count": 4}, today=TODAY) is None
    # same delta at real volume fires
    assert hiring_trend_signal("acme.com", {"count": 30}, {"count": 40}, today=TODAY) is not None
    # floor is config-overridable
    cand = hiring_trend_signal(
        "acme.com", {"count": 3}, {"count": 4}, today=TODAY, min_count=1
    )
    assert cand is not None


def test_stats_key_is_domain():
    assert stats_key("acme.com") == "acme.com"


def test_stats_roundtrip(tmp_path):
    path = tmp_path / "stats.json"
    save_stats({"acme.com": {"count": 10}}, path)
    assert load_stats(path) == {"acme.com": {"count": 10}}


def test_load_stats_missing_or_corrupt(tmp_path):
    assert load_stats(tmp_path / "nope.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert load_stats(bad) == {}


# ── runner integration: paginated ATS must store the FULL page count ──────

class _FakeFetch:
    def __init__(self, by_url: dict[str, bytes]):
        self.by_url = by_url

    def get(self, task, *, etag=None, last_modified=None):
        from src.core.http import FetchResult

        body = self.by_url[task.url]
        doc = Document(
            doc_id=f"d:{task.url}", source=task.source, url=task.url,
            domain=task.domain, body=body, status=200,
        )
        return FetchResult(True, 200, doc, False, None, 1)


class _PaginatedATS(SourceAdapter):
    """Two-page ATS board: page 1 plans a follow task, page 2 is final."""

    key = "ats_smartrecruiters"
    tier = "http"
    cadence_hours = 24
    requires = ("ats_token",)

    def plan(self, account, cursor):
        url = "https://jobs.test/p1" if not cursor else "https://jobs.test/p2"
        return [FetchTask(source=self.key, url=url, domain=account.domain)]

    def parse(self, doc, account, task_meta):
        return []

    def harvest_jobs(self, doc, account, task_meta):
        if doc.url.endswith("/p1"):
            return [
                JobPost(external_id=f"j{i}", title=f"Role {i}",
                        url=f"{doc.url}/job{i}", posted_at=None)
                for i in range(2)
            ]
        return [
            # distinct ids — page 2 must ADD 3 jobs, not overwrite page 1's
            JobPost(external_id=f"j{i}", title=f"Role {i}",
                    url=f"{doc.url}/job{i}", posted_at=None)
            for i in range(2, 5)
        ]

    def follow_tasks(self, doc, account, task_meta):
        if doc.url.endswith("/p1"):
            return [FetchTask(source=self.key, url="https://jobs.test/p2",
                              domain=account.domain)]
        return []


def test_paginated_ats_stores_full_open_role_count(tmp_path, monkeypatch):
    """MAJOR regression: on a follow-paginated ATS source, EVERY page's jobs
    must land in the hiring-trend stats (and the open-role snapshot) — not
    just the final page (which previously stored total-minus-page-1)."""
    import src.sources.jobsignals.trend as trend_mod
    from src.core.config import Config
    from src.core.db import Database
    from src.core.rawstore import RawStore
    from src.core.runlog import RunContext
    from src.identity.registry import AccountRegistry
    from src.pipeline.runner import CollectorRunner
    from src.signals.store import SignalStore
    from src.signals.taxonomy import Taxonomy

    saved: dict = {}
    monkeypatch.setattr(trend_mod, "load_stats", lambda *a, **k: {})
    monkeypatch.setattr(
        trend_mod, "save_stats", lambda stats, *a, **k: saved.update(stats)
    )

    db = Database(tmp_path / "s.db")
    cfg = Config()
    cfg.http.max_workers = 1
    cfg.http.respect_robots = False
    store = RawStore(db, tmp_path / "raw")
    tax = Taxonomy.load()
    ctx = RunContext(db, "collect")
    ctx.__enter__()
    fetch = _FakeFetch({
        "https://jobs.test/p1": b"<page1>",
        "https://jobs.test/p2": b"<page2>",
    })
    runner = CollectorRunner(
        cfg, db, AccountRegistry(db), store, fetch,
        SignalStore(db, tax), tax, ctx,
    )
    acct = Account(domain="acme.com", ats_vendor="smartrecruiters", ats_token="tok")
    runner.run([_PaginatedATS()], [acct], max_passes=2)
    ctx.__exit__(None, None, None)

    # 2 jobs on page 1 + 3 on page 2 = 5 open roles — the FULL count.
    assert saved.get("acme.com", {}).get("count") == 5
    snap = db.one("SELECT open_count FROM job_snapshots WHERE domain='acme.com'")
    assert snap is not None and snap["open_count"] == 5
    # No page-1 job may be marked closed by the final pass's mark_closed.
    open_rows = db.query("SELECT external_id FROM jobs WHERE domain='acme.com' AND closed_at IS NULL")
    assert {r["external_id"] for r in open_rows} == {"j0", "j1", "j2", "j3", "j4"}
