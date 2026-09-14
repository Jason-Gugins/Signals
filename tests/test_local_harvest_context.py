"""Task 4: local sources get the configured RawStore, and collect() keeps
the runner's per-source detail (outcomes + by_source).

Two seams are covered here:
  * CollectorRunner passes ``raw_store`` in the task_meta handed to a local
    tier adapter's ``local_harvest`` — the configured RawStore object itself,
    not the default ``data/raw`` store.
  * Orchestrator collect() folds the runner's RunnerStats through
    ``merge_runner_stats`` so per-source counters and ledger rows survive.
"""

from __future__ import annotations

from pathlib import Path

from src.core.config import Config
from src.core.db import Database
from src.core.models import Account, Document
from src.core.rawstore import RawStore
from src.core.runlog import RunContext
from src.identity.registry import AccountRegistry
from src.pipeline.orchestrator import Orchestrator, merge_runner_stats
from src.pipeline.runner import CollectorRunner, RunnerStats
from src.signals.store import SignalStore
from src.signals.taxonomy import Taxonomy
from src.sources.base import FetchTask, SignalCandidate, SourceAdapter


# --------------------------------------------------------------------------
# Change 2: merge_runner_stats
# --------------------------------------------------------------------------


def test_merge_runner_stats_sums_counters_and_appends_outcomes():
    outer = RunnerStats(tasks=1, fetched=2, cached=3, failed=4, skipped=5, candidates=6, signals_new=7)
    inner = RunnerStats(tasks=10, fetched=20, cached=30, failed=40, skipped=50, candidates=60, signals_new=70)
    inner.mark("needs", "acme.com", "ran_empty")

    merged = merge_runner_stats(outer, inner)

    assert merged is outer
    assert outer.tasks == 11
    assert outer.fetched == 22
    assert outer.cached == 33
    assert outer.failed == 44
    assert outer.skipped == 55
    assert outer.candidates == 66
    assert outer.signals_new == 77
    assert len(outer.outcomes) == 1
    assert outer.outcomes[0] is inner.outcomes[0]


def test_merge_runner_stats_merges_by_source():
    inner = RunnerStats()
    inner._src("jobsignals")["signals_new"] = 3
    inner._src("jobsignals")["fetched"] = 4
    inner._src("needs")["signals_new"] = 1

    outer = merge_runner_stats(RunnerStats(), inner)
    assert outer.by_source["jobsignals"]["signals_new"] == 3
    assert outer.by_source["jobsignals"]["fetched"] == 4
    assert outer.by_source["needs"]["signals_new"] == 1

    # Re-merging the same inner must accumulate, not clobber or lose keys.
    merge_runner_stats(outer, inner)
    assert outer.by_source["jobsignals"]["signals_new"] == 6
    assert outer.by_source["jobsignals"]["fetched"] == 8
    assert outer.by_source["needs"]["signals_new"] == 2
    assert set(outer.by_source) == {"jobsignals", "needs"}


# --------------------------------------------------------------------------
# Change 1: the runner hands local_harvest the configured RawStore
# --------------------------------------------------------------------------


class NullFetch:
    """plan() returns no tasks for the capture adapter, so this is never hit."""

    def __init__(self):
        self.calls: list[str] = []

    def get(self, task, *, etag=None, last_modified=None):  # pragma: no cover
        from src.core.http import FetchResult

        self.calls.append(task.url)
        doc = Document(
            doc_id="d", source=task.source, url=task.url, domain=task.domain, body=b"ok", status=200
        )
        return FetchResult(True, 200, doc, False, None, 1)


class LocalCaptureAdapter(SourceAdapter):
    key = "local_capture"
    tier = "local"
    cadence_hours = 24

    def __init__(self):
        self.kwargs: dict | None = None

    def plan(self, account, cursor):
        return []

    def parse(self, doc, account, task_meta):
        return []

    def local_harvest(self, *, db, account, today, task_meta):
        self.kwargs = {"db": db, "account": account, "today": today, "task_meta": task_meta}
        return []


def _runner_harness(tmp_path, adapters, accounts):
    """Same construction pattern as tests/test_runner.py::_harness."""
    db = Database(tmp_path / "s.db")
    cfg = Config()
    cfg.http.respect_robots = False
    store = RawStore(db, tmp_path / "raw")
    tax = Taxonomy.load()
    ctx = RunContext(db, "collect")
    ctx.__enter__()
    runner = CollectorRunner(
        cfg, db, AccountRegistry(db), store, NullFetch(), SignalStore(db, tax), tax, ctx
    )
    stats = runner.run(adapters, accounts, force=True)
    ctx.__exit__(None, None, None)
    return runner, stats, db, store


def test_local_harvest_receives_configured_raw_store(tmp_path):
    adapter = LocalCaptureAdapter()
    acct = Account(domain="acme.com", name="Acme")
    _, _, _, store = _runner_harness(tmp_path, [adapter], [acct])

    assert adapter.kwargs is not None, "local_harvest was never called"
    task_meta = adapter.kwargs["task_meta"]
    assert "raw_store" in task_meta, "runner did not pass the raw store to local_harvest"
    assert task_meta["raw_store"] is store, "raw_store is not the runner's configured store"
    # The configured store, not the default data/raw.
    assert Path(task_meta["raw_store"].raw_dir).resolve() == (tmp_path / "raw").resolve()
    assert Path(task_meta["raw_store"].raw_dir).resolve() != Path("data/raw").resolve()


# --------------------------------------------------------------------------
# Change 2 (call site): collect() exposes the runner's outcomes + by_source
# --------------------------------------------------------------------------


class _DummyAdapter(SourceAdapter):
    key = "dummy_http"
    tier = "http"
    cadence_hours = 1

    def plan(self, account, cursor):
        return [FetchTask(source=self.key, url=f"https://dummy.test/{account.domain}", domain=account.domain)]

    def parse(self, doc, account, task_meta):
        return [SignalCandidate("award", "2026-08-01", f"aw:{account.domain}", title="Award")]


class _DummyPlainFetch:
    def get(self, task, *, etag=None, last_modified=None):  # pragma: no cover
        raise AssertionError("collect() must not fetch when the runner is faked")


def _collect_orch(tmp_path):
    """Same construction pattern as tests/test_orchestrator.py::_orch."""
    cfg = Config()
    cfg.contact_email = "recon@example.com"
    cfg.http.respect_robots = False
    cfg.storage.db_path = str(tmp_path / "s.db")
    cfg.storage.raw_dir = str(tmp_path / "raw")
    cfg.storage.briefs_dir = str(tmp_path / "briefs")
    cfg.storage.export_dir = str(tmp_path / "exports")
    cfg.config_dir = "config"
    return Orchestrator(cfg, fetcher=_DummyPlainFetch(), adapters=[_DummyAdapter()])


def test_collect_exposes_runner_outcomes(tmp_path, monkeypatch):
    import src.pipeline.orchestrator as orch_mod

    inner = RunnerStats()
    row = inner.mark("dummy_http", "acme.com", "ok")
    inner._src("dummy_http")["signals_new"] = 2
    inner._src("dummy_http")["fetched"] = 5

    class _FakeRunner:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, adapters, accounts, **kwargs):
            return inner

    monkeypatch.setattr(orch_mod, "CollectorRunner", _FakeRunner)

    orch = _collect_orch(tmp_path)
    stats = orch.collect(force=True)

    assert stats.outcomes == [row]
    assert stats.outcomes[0] is row
    assert stats.by_source["dummy_http"]["signals_new"] == 2
    assert stats.by_source["dummy_http"]["fetched"] == 5
