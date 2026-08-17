"""Offline end-to-end pipeline against fixtures using real adapters."""

from __future__ import annotations

from pathlib import Path

from src.core.config import Config
from src.core.http import FetchResult
from src.core.models import Account, Document
from src.identity.edgar_ids import SUBMISSIONS_URL, pad_cik
from src.pipeline.orchestrator import Orchestrator
from src.sources.ats.collector import GreenhouseSource
from src.sources.jobsignals.collector import JobSignalsSource
from src.sources.news.collector import NewsRssSource
from src.sources.news.feeds import bing_news_url, google_news_url
from src.sources.owned.collector import OwnedIntentSource
from src.sources.sec.collector import SecEdgarSource


FIX = Path(__file__).resolve().parent / "fixtures"


class FakeFetch:
    def __init__(self, payloads: dict[str, bytes]):
        self.payloads = payloads

    def get(self, task, *, etag=None, last_modified=None):
        body = self.payloads.get(task.url, b"")
        doc = Document(doc_id=task.url[-16:], source=task.source, url=task.url, domain=task.domain, body=body, status=200)
        return FetchResult(True, 200, doc, False, None, 1)


def test_e2e_offline_twice(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    inbox = tmp_path / "data" / "inbox" / "owned"
    inbox.mkdir(parents=True)
    (inbox / "visits.csv").write_text((FIX / "owned" / "visits.csv").read_text(encoding="utf-8"), encoding="utf-8")

    cik = pad_cik("0001234567")
    sec_url = SUBMISSIONS_URL.format(cik10=cik)
    gh_url = "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"
    payloads = {
        sec_url: (FIX / "sec" / "submissions_sample.json").read_bytes(),
        gh_url: (FIX / "ats" / "greenhouse_jobs.json").read_bytes(),
        google_news_url("Acme"): (FIX / "news" / "google_news.xml").read_bytes(),
        bing_news_url("Acme"): (FIX / "news" / "google_news.xml").read_bytes(),
    }

    cfg = Config()
    cfg.contact_email = "recon@example.com"
    cfg.http.respect_robots = False
    cfg.storage.db_path = str(tmp_path / "s.db")
    cfg.storage.raw_dir = str(tmp_path / "raw")
    cfg.storage.briefs_dir = str(tmp_path / "briefs")
    cfg.storage.export_dir = str(tmp_path / "ex")
    cfg.config_dir = str(Path(__file__).resolve().parents[1] / "config")

    adapters = [SecEdgarSource(), GreenhouseSource(), NewsRssSource(), OwnedIntentSource(), JobSignalsSource()]
    orch = Orchestrator(cfg, fetcher=FakeFetch(payloads), adapters=adapters)
    orch.registry.upsert(
        Account(domain="acme.com", name="Acme", cik="0001234567", ats_vendor="greenhouse", ats_token="acme", industry="Software", employee_count=200)
    )
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
