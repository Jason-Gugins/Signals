"""Offline end-to-end pipeline against fixtures."""

from __future__ import annotations

from pathlib import Path

from src.core.config import Config
from src.core.models import Account, Document
from src.core.http import FetchResult
from src.pipeline.orchestrator import Orchestrator
from src.sources.ats.greenhouse import parse_greenhouse
from src.sources.base import FetchTask, SignalCandidate, SourceAdapter
from src.sources.news.classify import classify_news
from src.sources.news.feeds import NewsItem, parse_feed
from src.sources.owned.ingest import aggregate_intent
from src.sources.sec.parse_8k import classify_form
from src.sources.sec.parse_submissions import parse_submissions


FIX = Path("tests/fixtures")


class SecFix(SourceAdapter):
    key = "sec_edgar"
    tier = "http"

    def plan(self, account, cursor):
        return [FetchTask(source=self.key, url="https://data.sec.gov/submissions/CIK0001234567.json", domain=account.domain)]

    def parse(self, doc, account, task_meta):
        out = [
            SignalCandidate("ipo_filing", "2026-07-01", "sec:s1", title="S-1", confidence=0.9),
            SignalCandidate("annual_report_10k", "2026-03-01", "sec:10k", title="10-K", confidence=0.8),
            SignalCandidate("funding_form_d", "2026-06-01", "sec:d", title="Form D", confidence=0.85),
        ]
        today = __import__("datetime").date(2026, 8, 16)
        if doc.body:
            try:
                _, filings = parse_submissions(doc.body)
                for f in filings[:20]:
                    out.extend(classify_form(f, today=today))
            except Exception:
                pass
        return out


class GhFix(SourceAdapter):
    key = "greenhouse"
    tier = "http"

    def plan(self, account, cursor):
        return [FetchTask(source=self.key, url="https://boards-api.greenhouse.io/v1/boards/acme/jobs", domain=account.domain)]

    def parse(self, doc, account, task_meta):
        jobs = parse_greenhouse(doc.body)
        return [
            SignalCandidate("hiring_surge", "2026-08-01", "gh:surge", title=f"{len(jobs)} jobs", confidence=0.7, evidence_data={"department": "Sales", "open_count": len(jobs)})
        ] if jobs else []


class NewsFix(SourceAdapter):
    key = "news_rss"
    tier = "http"

    def plan(self, account, cursor):
        return [FetchTask(source=self.key, url="https://acme.com/feed", domain=account.domain)]

    def parse(self, doc, account, task_meta):
        items = parse_feed(doc.body)
        out = [
            SignalCandidate("product_launch", "2026-08-02", "blog:widget", title="Introducing Widget", url="https://acme.com/blog/widget", confidence=0.7),
        ]
        for it in items:
            cand = classify_news(it, account, today=__import__("datetime").date(2026, 8, 16))
            if cand:
                out.append(cand)
        return out


class OwnedFix(SourceAdapter):
    key = "owned_intent"
    tier = "local"

    def plan(self, account, cursor):
        return [FetchTask(source=self.key, url="file://owned", domain=account.domain)]

    def parse(self, doc, account, task_meta):
        import yaml

        rules = yaml.safe_load(Path("config/owned_pages.yaml").read_text(encoding="utf-8"))
        from src.sources.owned.ingest import read_drop

        rows = read_drop(str(FIX / "owned" / "visits.csv"))
        pairs = aggregate_intent(rows, today=__import__("datetime").date(2026, 8, 16), page_rules=rules)
        return [c for d, c in pairs]


class FakeFetch:
    def __init__(self):
        self.payloads = {
            "https://data.sec.gov/submissions/CIK0001234567.json": (FIX / "sec" / "submissions_sample.json").read_bytes(),
            "https://boards-api.greenhouse.io/v1/boards/acme/jobs": (FIX / "ats" / "greenhouse_jobs.json").read_bytes(),
            "https://acme.com/feed": (FIX / "news" / "company_blog.xml").read_bytes(),
            "file://owned": b"local",
        }

    def get(self, task, *, etag=None, last_modified=None):
        body = self.payloads[task.url]
        doc = Document(doc_id=task.url[-12:], source=task.source, url=task.url, domain=task.domain, body=body, status=200)
        return FetchResult(True, 200, doc, False, None, 1)


def test_e2e_offline_twice(tmp_path):
    cfg = Config()
    cfg.contact_email = "recon@example.com"
    cfg.http.respect_robots = False
    cfg.storage.db_path = str(tmp_path / "s.db")
    cfg.storage.raw_dir = str(tmp_path / "raw")
    cfg.storage.briefs_dir = str(tmp_path / "briefs")
    cfg.storage.export_dir = str(tmp_path / "ex")
    cfg.config_dir = "config"
    orch = Orchestrator(cfg, fetcher=FakeFetch(), adapters=[SecFix(), GhFix(), NewsFix(), OwnedFix()])
    orch.registry.upsert(Account(domain="acme.com", name="Acme", cik="0001234567", industry="Software", employee_count=200))
    s1 = orch.collect(force=True)
    assert s1.signals_new >= 6
    sources = {r["source"] for r in orch.db.query("SELECT DISTINCT source FROM signals")}
    assert len(sources) >= 4
    scored = orch.score()
    assert scored["scored"] == 1
    acct = orch.registry.get("acme.com")
    assert acct.score and acct.score > 0 and acct.tier
    paths = orch.brief(domains=["acme.com"], tier_max=4)
    text = Path(paths[0]).read_text(encoding="utf-8")
    assert "## Why now (top signals)" in text
    assert "## Stacked plays" in text
    s2 = orch.collect(force=True)
    assert s2.signals_new == 0
    text2 = Path(orch.brief(domains=["acme.com"], tier_max=4)[0]).read_text(encoding="utf-8")
    assert text == text2
