from src.core.config import Config
from src.core.db import Database
from src.core.models import Account
from src.identity.registry import AccountRegistry
from src.pipeline.health import doctor, render_status, status_report


def test_status_and_doctor(tmp_path, monkeypatch):
    db = Database(tmp_path / "s.db")
    AccountRegistry(db).upsert(Account(domain="acme.com", name="Acme", tier=1))
    report = status_report(db, taxonomy=None)
    assert "accounts" in report and "signals" in report and "sources" in report
    text = render_status(report)
    assert "accounts" in text
    assert render_status(report) == text

    cfg = Config()
    cfg.contact_email = None
    cfg.config_dir = str(tmp_path / "cfg")
    cfg.storage.raw_dir = str(tmp_path / "raw")
    cfg.storage.export_dir = str(tmp_path / "ex")
    cfg.storage.briefs_dir = str(tmp_path / "br")
    cfg.external_dbs.linkedin_db = str(tmp_path / "missing.db")
    cfg.external_dbs.repvue_db = str(tmp_path / "missing2.db")
    calls = []

    class BoomClient:
        def __init__(self, *a, **k):
            calls.append("net")
        def head(self, *a, **k):
            return None

    monkeypatch.setattr("httpx.Client", BoomClient)
    rows = doctor(cfg, db, check_network=False)
    assert calls == []
    statuses = {s for _, s, _ in rows}
    assert statuses <= {"OK", "WARN", "FAIL"}
    assert any(n == "contact_email" and s == "FAIL" for n, s, _ in rows)
    assert any(n == "linkedin_db" and s == "WARN" for n, s, _ in rows)
    assert all(s in {"OK", "WARN", "FAIL"} for _, s, _ in rows)
