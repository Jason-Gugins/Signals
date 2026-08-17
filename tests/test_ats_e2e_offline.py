"""Offline ATS path: resolve fixture → harvest → jobsignals."""

from __future__ import annotations

from pathlib import Path

from src.core.config import Config
from src.core.http import FetchResult
from src.core.models import Account, Document
from src.pipeline.orchestrator import Orchestrator
from src.sources.ats.collector import GreenhouseSource
from src.sources.jobsignals.collector import JobSignalsSource

FIX = Path(__file__).resolve().parent / "fixtures"


class Fake:
    def __init__(self, payloads):
        self.payloads = payloads

    def get(self, task, **kw):
        body = self.payloads.get(task.url, b"")
        doc = Document(
            doc_id=task.url[-16:],
            source=task.source,
            url=task.url,
            domain=task.domain,
            body=body,
            status=200,
        )
        return FetchResult(True, 200, doc, False, None, 1)


def test_ats_resolve_collect_jobsignals(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    careers = (FIX / "ats" / "careers_greenhouse.html").read_bytes()
    jobs = (FIX / "ats" / "greenhouse_jobs.json").read_bytes()
    cfg = Config()
    cfg.contact_email = "recon@example.com"
    cfg.http.respect_robots = False
    cfg.storage.db_path = str(tmp_path / "s.db")
    cfg.storage.raw_dir = str(tmp_path / "raw")
    cfg.storage.briefs_dir = str(tmp_path / "briefs")
    cfg.storage.export_dir = str(tmp_path / "ex")
    cfg.config_dir = str(Path(__file__).resolve().parents[1] / "config")
    payloads = {
        "https://acme.com/careers": careers,
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true": jobs,
    }
    orch = Orchestrator(cfg, fetcher=Fake(payloads), adapters=[GreenhouseSource(), JobSignalsSource()])
    orch.registry.upsert(Account(domain="acme.com", name="Acme", careers_url="https://acme.com/careers"))
    resolved = orch.resolve()
    assert resolved["ats"] == 1
    acct = orch.registry.get("acme.com")
    assert acct.ats_vendor == "greenhouse"
    token = acct.ats_token
    payloads[f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"] = jobs
    orch.collect(force=True)
    n_jobs = orch.db.one("SELECT COUNT(*) AS n FROM jobs")["n"]
    assert n_jobs >= 5
    types = {r["signal_type"] for r in orch.db.query("SELECT signal_type FROM signals")}
    assert "leadership_job_open" in types
    assert orch.db.one("SELECT COUNT(*) AS n FROM job_snapshots")["n"] >= 1
