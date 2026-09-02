"""Task 10/11 — doctor startup validation: config lint, engine, browser.

doctor() must surface rows for these checks and never raise, even when the
underlying config is broken or optional components (signals_antibot engine,
playwright chromium) are missing.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.config import Config
from src.core.db import Database
from src.pipeline.health import doctor


def _make_config(tmp_path):
    cfg = Config()
    cfg.contact_email = "ops@example.com"
    cfg.config_dir = str(tmp_path / "cfg")
    cfg.storage.raw_dir = str(tmp_path / "raw")
    cfg.storage.export_dir = str(tmp_path / "ex")
    cfg.storage.briefs_dir = str(tmp_path / "br")
    cfg.external_dbs.linkedin_db = str(tmp_path / "missing.db")
    cfg.external_dbs.repvue_db = str(tmp_path / "missing2.db")
    return cfg


def test_doctor_reports_config_lint_engine_browser_rows(tmp_path):
    db = Database(tmp_path / "s.db")
    cfg = _make_config(tmp_path)
    rows = doctor(cfg, db, check_network=False)
    names = [n for n, _, _ in rows]
    assert "config_lint" in names
    assert "antibot_engine" in names
    assert "browser" in names


def test_doctor_never_raises_on_broken_config(tmp_path):
    db = Database(tmp_path / "s.db")
    cfg = _make_config(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("yaml is broken")

    cfg.load_yaml = boom
    rows = doctor(cfg, db, check_network=False)  # must not raise
    names = [n for n, _, _ in rows]
    assert "config_lint" in names
    assert "config" in names
    assert all(s in {"OK", "WARN", "FAIL"} for _, s, _ in rows)


def test_engine_absent_is_warn_not_fail(tmp_path, monkeypatch):
    db = Database(tmp_path / "s.db")
    cfg = _make_config(tmp_path)

    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "signals_antibot":
            raise ImportError("no engine")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    rows = doctor(cfg, db, check_network=False)
    engine = [r for r in rows if r[0] == "antibot_engine"]
    assert engine and engine[0][1] == "WARN", engine


def test_browser_absent_is_warn_not_fail(tmp_path, monkeypatch):
    db = Database(tmp_path / "s.db")
    cfg = _make_config(tmp_path)

    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "playwright" or name.startswith("playwright."):
            raise ImportError("no playwright")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    rows = doctor(cfg, db, check_network=False)
    browser = [r for r in rows if r[0] == "browser"]
    assert browser and browser[0][1] == "WARN", browser
