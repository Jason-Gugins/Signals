"""Task 6 (P3): raw_quota row in status_report and doctor.

disabled -> nothing; under quota -> OK; over quota -> WARN.
"""

from __future__ import annotations

from src.core.config import Config
from src.core.db import Database
from src.core.rawstore import RawStore
from src.pipeline.health import doctor, status_report

MB = 1024 * 1024


def _db_with_raw(tmp_path, body_mb: float):
    db = Database(tmp_path / "s.db")
    store = RawStore(db, raw_dir=tmp_path / "raw")
    if body_mb:
        store.put(
            source="a",
            url="https://e/1",
            body=b"x" * int(body_mb * MB),
            content_type="text/plain",
            status=200,
        )
    return db


def _config(tmp_path, quota_mb):
    cfg = Config()
    cfg.contact_email = None
    cfg.config_dir = str(tmp_path / "cfg")
    cfg.storage.raw_dir = str(tmp_path / "raw")
    cfg.storage.export_dir = str(tmp_path / "ex")
    cfg.storage.briefs_dir = str(tmp_path / "br")
    cfg.storage.raw_quota_mb = quota_mb
    cfg.external_dbs.linkedin_db = str(tmp_path / "missing.db")
    cfg.external_dbs.repvue_db = str(tmp_path / "missing2.db")
    return cfg


def _doctor_rows(cfg, db):
    return {name: status for name, status, _ in doctor(cfg, db, check_network=False)}


def test_status_disabled_no_raw_quota_entry(tmp_path):
    db = _db_with_raw(tmp_path, 3.0)
    storage = status_report(db, taxonomy=None, raw_quota_mb=None)["storage"]
    assert "raw_quota" not in storage
    assert storage["raw_mb"] > 0


def test_status_under_is_ok_over_is_warn(tmp_path):
    db = _db_with_raw(tmp_path, 3.0)
    ok = status_report(db, taxonomy=None, raw_quota_mb=100.0)["storage"]["raw_quota"]
    assert ok == {"status": "OK", "used_mb": 3.0, "quota_mb": 100.0}
    warn = status_report(db, taxonomy=None, raw_quota_mb=2.0)["storage"]["raw_quota"]
    assert warn["status"] == "WARN"


def test_doctor_disabled_no_row_under_ok_over_warn(tmp_path):
    db = _db_with_raw(tmp_path, 3.0)
    assert "raw_quota" not in _doctor_rows(_config(tmp_path, None), db)
    assert _doctor_rows(_config(tmp_path, 100.0), db)["raw_quota"] == "OK"
    assert _doctor_rows(_config(tmp_path, 2.0), db)["raw_quota"] == "WARN"
