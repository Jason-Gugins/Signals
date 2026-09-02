"""Task 2: sources.yaml config lint — doctor warns on unconsumed keys."""

from src.pipeline.health import doctor, lint_source_config


class FakeAdapter:
    key = "fake_src"
    cadence_hours = 24
    requires = []


def test_clean_config_no_warnings():
    cfg = {
        "sources": {
            "fake_src": {"enabled": True, "cadence_hours": 12, "rate_per_host": 1.0},
        }
    }
    assert lint_source_config(cfg, {"fake_src": FakeAdapter}) == []


def test_bogus_key_warns_once():
    cfg = {
        "sources": {
            "fake_src": {"enabled": True, "bogus_key": 1},
        }
    }
    problems = lint_source_config(cfg, {"fake_src": FakeAdapter})
    assert len(problems) == 1
    source_key, problem = problems[0]
    assert source_key == "fake_src"
    assert "bogus_key" in problem and "fake_src" in problem


def test_allowlisted_keys_do_not_warn():
    cfg = {
        "sources": {
            "google_news": {"enabled": True, "serp_keywords": ["a"]},
            "company_feed": {"enabled": True, "blog_feed_url": "x"},
            "appstore_reviews": {"enabled": True, "app_store_id": "123"},
            "bbb_profile": {"enabled": True, "extra_data": {"bbb_url": "u"}},
            "jobsignals": {"enabled": True, "surge_min_roles": 5, "surge_window_days": 60},
        }
    }
    # allowlisted sources with no registered adapter still must not warn
    problems = lint_source_config(cfg, {})
    assert problems == []


def test_doctor_aggregated_check(tmp_path):
    from src.core.config import Config
    from src.core.db import Database

    cfg = Config()
    cfg.contact_email = "x@y.z"
    cfg.config_dir = str(tmp_path / "cfg")
    cfg.storage.raw_dir = str(tmp_path / "raw")
    cfg.storage.export_dir = str(tmp_path / "ex")
    cfg.storage.briefs_dir = str(tmp_path / "br")
    cfg.external_dbs.linkedin_db = str(tmp_path / "m1.db")
    cfg.external_dbs.repvue_db = str(tmp_path / "m2.db")

    real_load = Config.load_yaml

    def fake_load(self, name):
        if name == "sources":
            return {"sources": {"fake_src": {"enabled": True, "bogus_key": 1}}}
        return real_load(self, name)

    import unittest.mock as mock

    with mock.patch.object(Config, "load_yaml", fake_load):
        rows = doctor(cfg, Database(tmp_path / "s.db"), check_network=False)
    lint_rows = [(n, s, d) for n, s, d in rows if n == "config_lint"]
    assert len(lint_rows) == 1
    name, status, detail = lint_rows[0]
    assert status == "WARN"
    assert "fake_src.bogus_key" in detail
