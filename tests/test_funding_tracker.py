"""Tests for FundingTracker offline loop."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from src.core.config import Config
from src.core.db import Database
from src.core.http import FetchResult
from src.core.models import Account, Document
from src.core.rawstore import RawStore
from src.identity.edgar_ids import SUBMISSIONS_URL, pad_cik
from src.identity.registry import AccountRegistry
from src.pipeline.funding import FundingTracker, homepage_url, plan_company_queries, plan_funding
from src.signals.store import SignalStore
from src.signals.taxonomy import Taxonomy
from src.sources.sec.formd_filter import FormDFilter
from src.sources.sec.fts import hit_to_filing, parse_fts_response
from src.sources.sec.page_peel import peel_legal_names, peel_page


FIX = Path(__file__).resolve().parent / "fixtures"
TODAY = date(2026, 8, 22)


class FakeFetch:
    def __init__(self, payloads: dict[str, bytes]):
        self.payloads = payloads
        self.urls: list[str] = []

    def get(self, task, *, etag=None, last_modified=None):
        self.urls.append(task.url)
        body = self.payloads.get(task.url)
        if body is None:
            return FetchResult(False, 404, None, False, "no fixture", 1)
        doc = Document(
            doc_id=task.url[-16:],
            source=task.source,
            url=task.url,
            domain=task.domain,
            body=body,
            status=200,
        )
        return FetchResult(True, 200, doc, False, None, 1)


def _tracker(tmp_path, payloads):
    cfg = Config()
    cfg.contact_email = "recon@example.com"
    cfg.http.respect_robots = False
    cfg.storage.db_path = str(tmp_path / "s.db")
    cfg.storage.raw_dir = str(tmp_path / "raw")
    cfg.storage.export_dir = str(tmp_path / "ex")
    cfg.config_dir = str(Path(__file__).resolve().parents[1] / "config")
    db = Database(cfg.storage.db_path)
    return FundingTracker(
        cfg,
        db,
        AccountRegistry(db),
        RawStore(db, cfg.storage.raw_dir),
        FakeFetch(payloads),
        SignalStore(db, Taxonomy.load()),
        Taxonomy.load(),
    )


def _recent_payloads() -> dict[str, bytes]:
    fts = (FIX / "sec" / "fts_formd_recent.json").read_bytes()
    xml = (FIX / "sec" / "form_d_primary_doc.xml").read_bytes()
    tasks = plan_funding("recent", today=TODAY, days=30, size=100, limit=3)
    payloads = {tasks[0].url: fts}
    for hit in parse_fts_response(fts):
        filing = hit_to_filing(hit)
        if filing:
            payloads[filing.archive_url] = xml
    return payloads


def test_tracker_recent_persists_and_writes_csv(tmp_path):
    tr = _tracker(tmp_path, _recent_payloads())
    stats = tr.run("recent", today=TODAY, days=30, limit=3, filt=FormDFilter(), persist=True)
    assert stats.kept >= 1
    assert stats.signals_new >= 1
    rows = tr.db.query("SELECT * FROM signals WHERE signal_type='funding_form_d'")
    assert rows
    assert all(r["domain"].endswith(".edgar") or r["domain"] == "acme.com" for r in rows)
    assert stats.csv_path
    assert Path(stats.csv_path).is_file()
    text = Path(stats.csv_path).read_text(encoding="utf-8-sig")
    assert "funding_form_d" in text or "entity_name" in text


def test_tracker_unknown_domain_is_empty(tmp_path):
    tr = _tracker(tmp_path, {})
    stats = tr.run("company", today=TODAY, domain="missing.com")
    assert stats.kept == 0
    assert stats.signals_new == 0
    assert homepage_url("missing.com") in tr.fetcher.urls


def test_tracker_company_cik_uses_submissions(tmp_path):
    cik = pad_cik("0001234567")
    sub_url = SUBMISSIONS_URL.format(cik10=cik)
    xml = (FIX / "sec" / "form_d_primary_doc.xml").read_bytes()
    sub = (FIX / "sec" / "submissions_sample.json").read_bytes()
    from src.sources.sec.parse_submissions import parse_submissions

    _, filings = parse_submissions(sub)
    payloads = {sub_url: sub}
    for f in filings:
        if f.form == "D":
            payloads[f.archive_url] = xml
    tr = _tracker(tmp_path, payloads)
    tr.registry.upsert(Account(domain="acme.com", name="Acme", cik=cik))
    stats = tr.run("company", today=TODAY, cik=cik, filt=FormDFilter(), persist=True)
    assert stats.kept >= 1
    assert any(r["domain"] == "acme.com" for r in tr.db.query("SELECT domain FROM signals"))


def test_tracker_dry_run_fetches_nothing(tmp_path):
    tr = _tracker(tmp_path, _recent_payloads())
    stats = tr.run("recent", today=TODAY, dry_run=True, limit=3)
    assert stats.fetched == 0
    assert tr.fetcher.urls == []
    assert stats.hits >= 1


def test_orchestrator_funding_recent(tmp_path):
    from src.pipeline.orchestrator import Orchestrator, set_today
    from src.sources.sec.formd_source import SecFormDSource

    # Freeze the age reference through the supported injection hook. This test
    # used to pin the module's LOCAL clock (monkeypatching orchestrator.date);
    # Finding 7 routed this path through orchestrator._today(), so the hook is
    # what controls the reference now (tests/conftest.py resets it afterwards).
    set_today(TODAY)
    cfg = Config()
    cfg.contact_email = "recon@example.com"
    cfg.http.respect_robots = False
    cfg.storage.db_path = str(tmp_path / "s.db")
    cfg.storage.raw_dir = str(tmp_path / "raw")
    cfg.storage.briefs_dir = str(tmp_path / "br")
    cfg.storage.export_dir = str(tmp_path / "ex")
    cfg.config_dir = str(Path(__file__).resolve().parents[1] / "config")
    orch = Orchestrator(cfg, fetcher=FakeFetch(_recent_payloads()), adapters=[SecFormDSource()])
    stats = orch.funding("recent", days=30, limit=3)
    assert stats.kept >= 1
    assert stats.signals_new >= 1
    assert stats.csv_path


def _one_hit_fts(*, cik: str, adsh: str) -> bytes:
    return json.dumps(
        {
            "hits": {
                "total": {"value": 1, "relation": "eq"},
                "hits": [
                    {
                        "_source": {
                            "ciks": [cik],
                            "form": "D",
                            "adsh": adsh,
                            "file_date": "2026-04-01",
                            "display_names": ["fixture"],
                        }
                    }
                ],
            }
        }
    ).encode("utf-8")


def _peel_payloads(domain: str, html: bytes, xml: bytes, *, cik: str, adsh: str) -> dict[str, bytes]:
    names = peel_legal_names(peel_page(html, domain=domain), domain=domain)
    tasks = plan_company_queries(names, today=TODAY, limit=8)
    fts = _one_hit_fts(cik=cik, adsh=adsh)
    filing = hit_to_filing(parse_fts_response(fts)[0])
    payloads = {homepage_url(domain): html, filing.archive_url: xml}
    for t in tasks:
        payloads[t.url] = fts
    return payloads


def test_tracker_domain_peel_keeps_acme(tmp_path):
    html = b"<html><head><title>Acme</title></head><body></body></html>"
    xml = (FIX / "sec" / "form_d_primary_doc.xml").read_bytes()
    payloads = _peel_payloads("acme.com", html, xml, cik="0001234567", adsh="0001234567-26-000001")
    tr = _tracker(tmp_path, payloads)
    stats = tr.run("company", today=TODAY, domain="acme.com", persist=True)
    assert stats.kept >= 1
    rows = tr.db.query("SELECT domain FROM signals")
    assert any(r["domain"] == "acme.com" for r in rows)
    assert homepage_url("acme.com") in tr.fetcher.urls


def test_tracker_domain_peel_drops_surgical_safety(tmp_path):
    html = (FIX / "sec" / "pages" / "scanner.dev.html").read_bytes()
    xml = (FIX / "sec" / "form_d_primary_doc.xml").read_bytes().replace(
        b"<entityName>Acme Robotics Inc.</entityName>",
        b"<entityName>Surgical Safety Scanner, Inc.</entityName>",
    )
    payloads = _peel_payloads("scanner.dev", html, xml, cik="0001234567", adsh="0001234567-26-000001")
    tr = _tracker(tmp_path, payloads)
    tr.registry.upsert(Account(domain="scanner.dev", name="Scanner"))
    stats = tr.run("company", today=TODAY, domain="scanner.dev", persist=True)
    assert stats.kept == 0
    assert homepage_url("scanner.dev") in tr.fetcher.urls


def test_tracker_domain_dry_run_skips_homepage(tmp_path):
    tr = _tracker(tmp_path, {})
    stats = tr.run("company", today=TODAY, domain="acme.com", dry_run=True)
    assert stats.fetched == 0
    assert tr.fetcher.urls == []
    assert stats.hits >= 1


def _n_hit_fts(*, n: int, cik: str) -> bytes:
    hits = []
    for i in range(1, n + 1):
        hits.append(
            {
                "_source": {
                    "ciks": [cik],
                    "form": "D",
                    "adsh": f"{cik}-26-{i:06d}",
                    "file_date": "2026-04-01",
                    "display_names": ["fixture"],
                }
            }
        )
    return json.dumps({"hits": {"total": {"value": n, "relation": "eq"}, "hits": hits}}).encode("utf-8")


def test_tracker_common_token_query_caps_xml_follow(tmp_path):
    html = b"<html><head><title>Clay</title></head><body></body></html>"
    xml = (FIX / "sec" / "form_d_primary_doc.xml").read_bytes().replace(
        b"<entityName>Acme Robotics Inc.</entityName>",
        b"<entityName>OE VILLAGE OF WEST CLAY, LLC</entityName>",
    )
    fts = _n_hit_fts(n=8, cik="0001234567")
    names = peel_legal_names(peel_page(html, domain="clay.com"), domain="clay.com")
    tasks = plan_company_queries(names, today=TODAY, limit=100)
    payloads = {homepage_url("clay.com"): html}
    for t in tasks:
        payloads[t.url] = fts
    for hit in parse_fts_response(fts):
        filing = hit_to_filing(hit)
        if filing:
            payloads[filing.archive_url] = xml
    tr = _tracker(tmp_path, payloads)
    tr.run("company", today=TODAY, domain="clay.com", persist=True, limit=100)
    xml_gets = sum(1 for u in tr.fetcher.urls if u.endswith("primary_doc.xml"))
    assert xml_gets <= 5
