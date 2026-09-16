"""Tests for the stage orchestrator."""

from __future__ import annotations

from pathlib import Path

import respx
import httpx
import yaml

from src.core.config import Config
from src.core.db import Database
from src.core.models import Account, Document
from src.core.rawstore import RawStore
from src.pipeline.orchestrator import Orchestrator
from src.sources.base import FetchTask, SignalCandidate, SourceAdapter


class LocalAdapter(SourceAdapter):
    key = "local_ok"
    tier = "http"
    cadence_hours = 1

    def plan(self, account, cursor):
        return [FetchTask(source=self.key, url=f"https://local.test/{account.domain}", domain=account.domain)]

    def parse(self, doc, account, task_meta):
        return [SignalCandidate("award", "2026-08-01", f"aw:{account.domain}", title="Award")]


class FakeFetch:
    def __init__(self):
        self.calls = 0

    def get(self, task, *, etag=None, last_modified=None):
        from src.core.http import FetchResult

        self.calls += 1
        doc = Document(doc_id=f"doc-{task.domain}", source=task.source, url=task.url, domain=task.domain, body=b"ok", status=200)
        return FetchResult(True, 200, doc, False, None, 1)


def _orch(tmp_path, fetcher=None):
    cfg = Config()
    cfg.contact_email = "recon@example.com"
    cfg.http.respect_robots = False
    cfg.storage.db_path = str(tmp_path / "s.db")
    cfg.storage.raw_dir = str(tmp_path / "raw")
    cfg.storage.briefs_dir = str(tmp_path / "briefs")
    cfg.storage.export_dir = str(tmp_path / "exports")
    cfg.config_dir = "config"
    return Orchestrator(cfg, fetcher=fetcher or FakeFetch(), adapters=[LocalAdapter()])


def test_stages_write_runs_and_collect_idempotent(tmp_path):
    csv_path = tmp_path / "seed.csv"
    csv_path.write_text("domain,name\nacme.com,Acme\n", encoding="utf-8")
    orch = _orch(tmp_path)
    orch.seed(csv=str(csv_path), linkedin=False, repvue=False, cohort="t")
    collect1 = orch.collect(force=True)
    collect2 = orch.collect(force=True)
    assert collect1.signals_new >= 1
    assert collect2.signals_new == 0
    rows = orch.db.query("SELECT stage, status FROM runs ORDER BY started_at")
    stages = [r["stage"] for r in rows]
    assert "seed" in stages and "collect" in stages
    assert all(r["status"] in {"completed", "failed"} for r in rows)


def test_reparse_zero_network_same_ids(tmp_path):
    csv_path = tmp_path / "seed.csv"
    csv_path.write_text("domain,name\nacme.com,Acme\n", encoding="utf-8")
    orch = _orch(tmp_path)
    orch.seed(csv=str(csv_path), linkedin=False, repvue=False, cohort=None)
    orch.collect(force=True)
    ids = [r["signal_id"] for r in orch.db.query("SELECT signal_id FROM signals ORDER BY signal_id")]
    # put body into raw store so reparse can load it
    store = RawStore(orch.db, orch.config.storage.raw_dir)
    store.put(source="local_ok", url="https://local.test/acme.com", body=b"ok", content_type="text/plain", status=200, domain="acme.com")
    with respx.mock:
        respx.route().mock(side_effect=AssertionError("network"))
        stats = orch.reparse()
    ids2 = [r["signal_id"] for r in orch.db.query("SELECT signal_id FROM signals ORDER BY signal_id")]
    assert ids == ids2
    assert stats.signals_new == 0


def test_reparse_sec_submissions_does_not_need_empty_meta(tmp_path):
    from src.sources.sec.collector import SecEdgarSource

    cfg = Config()
    cfg.contact_email = "recon@example.com"
    cfg.http.respect_robots = False
    cfg.storage.db_path = str(tmp_path / "s.db")
    cfg.storage.raw_dir = str(tmp_path / "raw")
    cfg.storage.briefs_dir = str(tmp_path / "briefs")
    cfg.storage.export_dir = str(tmp_path / "exports")
    cfg.config_dir = "config"
    orch = Orchestrator(cfg, fetcher=FakeFetch(), adapters=[SecEdgarSource()])
    orch.registry.upsert(Account(domain="acme.com", name="Acme", cik="0001234567"))
    body = Path("tests/fixtures/sec/submissions_sample.json").read_bytes()
    RawStore(orch.db, orch.config.storage.raw_dir).put(
        source="sec_edgar",
        url="https://data.sec.gov/submissions/CIK0001234567.json",
        body=body,
        content_type="application/json",
        status=200,
        domain="acme.com",
    )
    stats = orch.reparse(sources=["sec_edgar"])
    assert stats.failed == 0
    assert stats.candidates > 0
    assert orch.db.one("SELECT COUNT(*) AS n FROM signals")["n"] > 0


def test_resolve_ats_writes_vendor(tmp_path):
    html = Path("tests/fixtures/ats/careers_greenhouse.html").read_bytes()

    class CareersFetch:
        def get(self, task, **kw):
            from src.core.http import FetchResult

            doc = Document(doc_id="c", source="ats_discovery", url=task.url, body=html, status=200)
            return FetchResult(True, 200, doc, False, None, 1)

    orch = _orch(tmp_path, fetcher=CareersFetch())
    orch.registry.upsert(Account(domain="acme.com", name="Acme", careers_url="https://acme.com/careers"))
    out = orch.resolve(ats=True, cik=False, feeds=False, icp=False)
    assert out["ats"] == 1
    acct = orch.registry.get("acme.com")
    assert acct.ats_vendor == "greenhouse"
    assert acct.ats_token == "acme"


def test_resolve_cik_from_tickers_fixture(tmp_path):
    body = Path("tests/fixtures/sec/company_tickers.json").read_bytes()
    from src.identity.edgar_ids import TICKERS_URL

    class TickersFetch:
        def get(self, task, **kw):
            from src.core.http import FetchResult

            assert task.url == TICKERS_URL
            doc = Document(doc_id="t", source="sec_edgar", url=task.url, body=body, status=200)
            return FetchResult(True, 200, doc, False, None, 1)

    orch = _orch(tmp_path, fetcher=TickersFetch())
    orch.registry.upsert(Account(domain="apple.com", name="Apple Inc.", ticker="AAPL"))
    out = orch.resolve(ats=False, cik=True, feeds=False, icp=False)
    assert out["cik"] == 1
    assert orch.registry.get("apple.com").cik == "0000320193"


def test_resolve_cik_skips_when_already_set(tmp_path):
    class Boom:
        def get(self, task, **kw):
            raise AssertionError("should not fetch")

    orch = _orch(tmp_path, fetcher=Boom())
    orch.registry.upsert(Account(domain="acme.com", name="Acme", cik="0001234567"))
    out = orch.resolve(ats=False, cik=True, feeds=False, icp=False)
    assert out["cik"] == 0


def test_resolve_feeds_writes_blog_url(tmp_path):
    html = Path("tests/fixtures/news/homepage_with_feed.html").read_bytes()

    class HomeFetch:
        def get(self, task, **kw):
            from src.core.http import FetchResult

            body = html if (task.url or "").rstrip("/") == "https://acme.com" else b""
            doc = Document(doc_id="h", source="feed_discovery", url=task.url, body=body, status=200)
            return FetchResult(True, 200, doc, False, None, 1)

    orch = _orch(tmp_path, fetcher=HomeFetch())
    orch.registry.upsert(Account(domain="acme.com", name="Acme"))
    out = orch.resolve(ats=False, cik=False, feeds=True, icp=False)
    assert out["feeds"] == 1
    assert orch.registry.get("acme.com").blog_feed_url == "https://acme.com/blog/feed"


def test_score_writes_history(tmp_path):
    csv_path = tmp_path / "seed.csv"
    csv_path.write_text("domain,name\nacme.com,Acme\n", encoding="utf-8")
    orch = _orch(tmp_path)
    orch.seed(csv=str(csv_path), linkedin=False, repvue=False, cohort=None)
    orch.collect(force=True)
    out = orch.score()
    assert out["scored"] >= 1
    acct = orch.registry.get("acme.com")
    assert acct.score is not None and acct.tier is not None
    hist = orch.db.query("SELECT * FROM score_history WHERE domain='acme.com'")
    assert hist


def test_run_all_continues_unless_strict(tmp_path):
    class Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("nope")

    orch = _orch(tmp_path)
    orch.collect = Boom  # type: ignore
    result = orch.run_all(strict=False, skip_seed=True)
    assert result["collect"] is None or result.get("errors")
    rows = [r for r in orch.db.query("SELECT * FROM runs") if r["stage"] == "collect"]
    assert any(r["status"] == "failed" for r in rows)
    try:
        orch.run_all(strict=True, skip_seed=True)
        raised = False
    except RuntimeError:
        raised = True
    assert raised


def test_collect_sec_formd_empty_registry_runs_tracker(tmp_path):
    from datetime import date as real_date

    from src.pipeline.funding import plan_funding
    from src.pipeline.orchestrator import set_today
    from src.sources.sec.formd_source import SecFormDSource
    from src.sources.sec.fts import hit_to_filing, parse_fts_response

    today = real_date(2026, 8, 22)
    # The age reference comes from the supported injection hook; this test used
    # to pin the module's LOCAL clock, which the _today()-routed path (Finding
    # 7) no longer reads (tests/conftest.py resets the hook afterwards).
    set_today(today)
    fts = Path("tests/fixtures/sec/fts_formd_recent.json").read_bytes()
    xml = Path("tests/fixtures/sec/form_d_primary_doc.xml").read_bytes()
    efts_url = plan_funding("recent", today=today, days=30, size=100, limit=100)[0].url
    payloads = {efts_url: fts}
    for hit in parse_fts_response(fts):
        filing = hit_to_filing(hit)
        if filing:
            payloads[filing.archive_url] = xml

    class MapFetch:
        def __init__(self):
            self.n = 0

        def get(self, task, **kw):
            from src.core.http import FetchResult

            self.n += 1
            body = payloads.get(task.url)
            if body is None:
                return FetchResult(False, 404, None, False, "miss", 1)
            doc = Document(doc_id=task.url[-12:], source=task.source, url=task.url, body=body, status=200)
            return FetchResult(True, 200, doc, False, None, 1)

    cfg = Config()
    cfg.contact_email = "recon@example.com"
    cfg.http.respect_robots = False
    cfg.storage.db_path = str(tmp_path / "s.db")
    cfg.storage.raw_dir = str(tmp_path / "raw")
    cfg.storage.briefs_dir = str(tmp_path / "briefs")
    cfg.storage.export_dir = str(tmp_path / "ex")
    cfg.config_dir = "config"
    fetch = MapFetch()
    orch = Orchestrator(cfg, fetcher=fetch, adapters=[SecFormDSource()])
    assert orch.registry.list_accounts() == []
    stats = orch.collect(sources=["sec_formd"], force=True)
    assert stats.signals_new >= 1
    first_n = fetch.n
    stats2 = orch.collect(sources=["sec_formd"], force=False)
    assert stats2.signals_new == 0
    assert fetch.n == first_n
    stats3 = orch.collect(sources=["sec_formd"], force=True)
    assert stats3.signals_new >= 0
    assert fetch.n > first_n


def test_reparse_sec_formd_xml_does_not_need_empty_meta(tmp_path):
    from src.sources.sec.formd_source import SecFormDSource

    cfg = Config()
    cfg.contact_email = "recon@example.com"
    cfg.http.respect_robots = False
    cfg.storage.db_path = str(tmp_path / "s.db")
    cfg.storage.raw_dir = str(tmp_path / "raw")
    cfg.storage.briefs_dir = str(tmp_path / "briefs")
    cfg.storage.export_dir = str(tmp_path / "ex")
    cfg.config_dir = "config"
    orch = Orchestrator(cfg, fetcher=FakeFetch(), adapters=[SecFormDSource()])
    orch.registry.upsert(Account(domain="acme.com", name="Acme Robotics Inc.", cik="0001234567"))
    body = Path("tests/fixtures/sec/form_d_primary_doc.xml").read_bytes()
    RawStore(orch.db, orch.config.storage.raw_dir).put(
        source="sec_formd",
        url="https://www.sec.gov/Archives/edgar/data/1234567/x/primary_doc.xml",
        body=body,
        content_type="application/xml",
        status=200,
        domain="acme.com",
    )
    stats = orch.reparse(sources=["sec_formd"])
    assert stats.failed == 0
    assert stats.candidates >= 1
    assert orch.db.one("SELECT COUNT(*) AS n FROM signals WHERE signal_type='funding_form_d'")["n"] >= 1


def test_pick_adapters_threads_include_disabled(monkeypatch):
    from src.core.config import Config
    from src.pipeline.orchestrator import Orchestrator
    from src.sources.base import SourceAdapter
    from src.sources.registry import SOURCES, register

    @register
    class OffAdapter(SourceAdapter):
        key = "dummy_pick_off"
        tier = "http"

        def plan(self, account, cursor):
            return []

        def parse(self, doc, account, task_meta):
            return []

    try:
        cfg = Config()
        monkeypatch.setattr(
            cfg, "load_yaml",
            lambda name: {"sources": {"dummy_pick_off": {"enabled": False}}},
        )
        orch = Orchestrator.__new__(Orchestrator)
        orch._adapters = None
        orch.config = cfg

        assert orch._pick_adapters(None) == []
        assert orch._pick_adapters(["dummy_pick_off"]) == []
        picked = orch._pick_adapters(["dummy_pick_off"], include_disabled={"dummy_pick_off"})
        assert [a.key for a in picked] == ["dummy_pick_off"]
    finally:
        SOURCES.pop("dummy_pick_off", None)
