"""Tests for the per-source execution ledger on RunnerStats."""

from __future__ import annotations

from src.core.models import Account
from src.pipeline.runner import RunnerStats
from tests.test_runner import _harness

LEDGER_KEYS = [
    "source",
    "key",
    "status",
    "reason",
    "tasks",
    "fetched",
    "cached",
    "failed",
    "candidates",
    "signals_new",
]
COUNTERS = ("tasks", "fetched", "cached", "failed", "candidates", "signals_new")


def test_out_row_shape_and_dedupe():
    stats = RunnerStats()
    row = stats._out("jobsignals", "acme.com")
    assert list(row.keys()) == LEDGER_KEYS
    assert row["source"] == "jobsignals"
    assert row["key"] == "acme.com"
    assert row["status"] == "ran_empty"
    assert row["reason"] is None
    for counter in COUNTERS:
        assert row[counter] == 0

    again = stats._out("jobsignals", "acme.com")
    assert len(stats.outcomes) == 1
    assert again is row


def test_mark_sets_status_and_reason():
    stats = RunnerStats()
    row = stats.mark("jobsignals", "acme.com", "failed", "boom")
    assert row["status"] == "failed"
    assert row["reason"] == "boom"
    same = stats.mark("jobsignals", "acme.com", "ran_data", "p2")
    assert same is row
    assert len(stats.outcomes) == 1
    assert row["status"] == "ran_data"
    assert row["reason"] == "p2"

    no_reason = stats.mark("jobsignals", "acme.com", "ran_data")
    assert no_reason is row
    assert no_reason["reason"] is None
    assert len(stats.outcomes) == 1


def test_mark_is_per_source_and_key():
    stats = RunnerStats()
    rows = [
        stats.mark(source, key, "ran_empty")
        for source in ("ok", "boom")
        for key in ("acme.com", "beta.com")
    ]
    assert len(stats.outcomes) == 4
    assert len({id(r) for r in rows}) == 4
    assert {(r["source"], r["key"]) for r in rows} == {
        ("ok", "acme.com"),
        ("ok", "beta.com"),
        ("boom", "acme.com"),
        ("boom", "beta.com"),
    }


def test_runner_records_ran_outcome_for_real_pair(tmp_path):
    from tests.test_runner import OkAdapter

    acct = Account(domain="acme.com", name="Acme")
    _, stats, _ = _harness(tmp_path, [acct], [OkAdapter()], {"https://ok.test/acme.com": b"ok"})
    rows = [r for r in stats.outcomes if r["source"] == "ok" and r["key"] == "acme.com"]
    assert len(rows) == 1
    assert rows[0]["status"] in ("ran_empty", "ran_data")
    assert rows[0]["status"] != "failed"


def test_runner_records_failed_when_plan_raises(tmp_path):
    from src.sources.base import SourceAdapter

    class PlanBoom(SourceAdapter):
        key = "planboom"
        tier = "http"

        def plan(self, account, cursor):
            raise RuntimeError("plan exploded")

        def parse(self, doc, account, task_meta):
            return []

    acct = Account(domain="acme.com", name="Acme")
    _, stats, _ = _harness(tmp_path, [acct], [PlanBoom()], {})
    rows = [r for r in stats.outcomes if r["source"] == "planboom" and r["key"] == "acme.com"]
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert isinstance(rows[0]["reason"], str) and rows[0]["reason"]