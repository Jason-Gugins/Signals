"""Tests for per-source coverage rows (the honesty ledger)."""

from __future__ import annotations

import pytest

from src.core.config import Config
from src.core.models import Account
from src.intel.coverage import build_coverage, coverage_summary
from src.sources.base import SourceAdapter
from src.sources.registry import SOURCES, register

ROW_KEYS = {
    "source",
    "key",
    "status",
    "reason",
    "tasks",
    "fetched",
    "cached",
    "failed",
    "candidates",
    "signals_new",
}


@pytest.fixture(autouse=True)
def _restore_registry():
    """Undo any dummy registration (and clobber) this test made."""
    snapshot = dict(SOURCES)
    yield
    for key in list(SOURCES):
        if key not in snapshot:
            SOURCES.pop(key, None)
    SOURCES.update(snapshot)


def _register_dummy(key: str, requires: tuple[str, ...] = ()):
    cls = type(
        "Dummy",
        (SourceAdapter,),
        {
            "key": key,
            "tier": "http",
            "requires": tuple(requires),
            "plan": lambda self, account, cursor: [],
            "parse": lambda self, doc, account, task_meta: [],
        },
    )
    register(cls)
    return cls


def _cfg(monkeypatch, sources: dict):
    cfg = Config()

    def fake_yaml(name: str):
        return {"sources": sources}

    monkeypatch.setattr(cfg, "load_yaml", fake_yaml)
    return cfg


def test_every_configured_source_appears_exactly_once(monkeypatch):
    _register_dummy("cov_alpha")
    _register_dummy("cov_beta")
    sources = {
        "cov_alpha": {"enabled": True},
        "cov_beta": {"enabled": True},
        "cov_disabled": {"enabled": False},
        "cov_ghost": {"enabled": True},  # never registered
    }
    cfg = _cfg(monkeypatch, sources)
    rows = build_coverage(config=cfg, account=Account(domain="acme.com"), outcomes=[])

    assert len(rows) == len(sources)
    assert {r["source"] for r in rows} == set(sources)
    assert [r["source"] for r in rows] == sorted(sources)
    for row in rows:
        assert set(row) == ROW_KEYS


def test_runner_outcome_wins_over_config_classification(monkeypatch):
    _register_dummy("cov_ran")
    sources = {"cov_ran": {"enabled": True}}
    cfg = _cfg(monkeypatch, sources)
    outcome = {
        "source": "cov_ran",
        "key": "acme.com",
        "status": "ran_empty",
        "reason": None,
        "tasks": 7,
        "fetched": 3,
        "cached": 1,
        "failed": 0,
        "candidates": 0,
        "signals_new": 0,
    }
    rows = build_coverage(
        config=cfg, account=Account(domain="acme.com"), outcomes=[outcome]
    )
    row = rows[0]
    assert row["status"] == "ran_empty"
    assert row["tasks"] == 7
    assert row["fetched"] == 3
    assert row["cached"] == 1


def test_disabled_key_is_opt_in_when_a_flag_exists(monkeypatch):
    sources = {"cov_optin": {"enabled": False}}
    cfg = _cfg(monkeypatch, sources)
    flag = "--include-disabled cov_optin"
    rows = build_coverage(
        config=cfg,
        account=Account(domain="acme.com"),
        outcomes=[],
        opt_in_flags={"cov_optin": flag},
    )
    row = rows[0]
    assert row["status"] == "opt_in"
    assert flag in row["reason"]


def test_disabled_key_without_a_flag_is_disabled_by_config(monkeypatch):
    sources = {"cov_off": {"enabled": False}}
    cfg = _cfg(monkeypatch, sources)
    rows = build_coverage(config=cfg, account=Account(domain="acme.com"), outcomes=[])
    row = rows[0]
    assert row["status"] == "disabled_by_config"
    assert "disabled" in row["reason"].lower()


def test_unregistered_key_is_not_registered(monkeypatch):
    sources = {"cov_no_adapter": {"enabled": True}}
    cfg = _cfg(monkeypatch, sources)
    rows = build_coverage(config=cfg, account=Account(domain="acme.com"), outcomes=[])
    assert rows[0]["status"] == "not_registered"


def test_ats_vendor_mismatch_is_ineligible_ats_vendor(monkeypatch):
    _register_dummy("ats_dummyvendor")
    sources = {"ats_dummyvendor": {"enabled": True}}
    cfg = _cfg(monkeypatch, sources)
    account = Account(domain="acme.com", ats_vendor="lever")
    rows = build_coverage(config=cfg, account=account, outcomes=[])
    row = rows[0]
    assert row["status"] == "ineligible_ats_vendor"
    assert "ats_dummyvendor" in row["reason"]
    assert "lever" in row["reason"]

    _register_dummy("ats_careers_page")
    sources2 = {"ats_careers_page": {"enabled": True}}
    cfg2 = _cfg(monkeypatch, sources2)
    account2 = Account(domain="acme.com", ats_vendor="greenhouse")
    rows2 = build_coverage(config=cfg2, account=account2, outcomes=[])
    assert rows2[0]["status"] == "ineligible_ats_vendor"


def test_missing_required_field_names_the_field(monkeypatch):
    _register_dummy("cov_needs_cik", requires=("cik",))
    sources = {"cov_needs_cik": {"enabled": True}}
    cfg = _cfg(monkeypatch, sources)
    rows = build_coverage(config=cfg, account=Account(domain="acme.com"), outcomes=[])
    row = rows[0]
    assert row["status"] == "missing_requires"
    assert "cik" in row["reason"]


def test_eligible_source_without_outcome_is_not_selected_not_ran(monkeypatch):
    _register_dummy("cov_eligible")
    sources = {"cov_eligible": {"enabled": True}}
    cfg = _cfg(monkeypatch, sources)
    rows = build_coverage(config=cfg, account=Account(domain="acme.com"), outcomes=[])
    row = rows[0]
    assert row["status"] == "not_selected"
    assert row["status"] != "ran_empty"


def test_fanout_global_key_is_preserved(monkeypatch):
    _register_dummy("cov_fanout")
    sources = {"cov_fanout": {"enabled": True}}
    cfg = _cfg(monkeypatch, sources)
    outcome = {
        "source": "cov_fanout",
        "key": "global",
        "status": "ran_data",
        "reason": None,
        "tasks": 1,
        "fetched": 1,
        "cached": 0,
        "failed": 0,
        "candidates": 2,
        "signals_new": 2,
    }
    rows = build_coverage(
        config=cfg, account=Account(domain="acme.com"), outcomes=[outcome]
    )
    assert rows[0]["key"] == "global"


def test_coverage_summary_counts_by_status(monkeypatch):
    _register_dummy("cov_sum_ran")
    _register_dummy("cov_sum_eligible")
    sources = {
        "cov_sum_ran": {"enabled": True},
        "cov_sum_eligible": {"enabled": True},
        "cov_sum_off": {"enabled": False},
        "cov_sum_ghost": {"enabled": True},
    }
    cfg = _cfg(monkeypatch, sources)
    outcome = {
        "source": "cov_sum_ran",
        "key": "acme.com",
        "status": "ran_empty",
        "reason": None,
        "tasks": 0,
        "fetched": 0,
        "cached": 0,
        "failed": 0,
        "candidates": 0,
        "signals_new": 0,
    }
    rows = build_coverage(
        config=cfg, account=Account(domain="acme.com"), outcomes=[outcome]
    )
    summary = coverage_summary(rows)
    assert sum(summary.values()) == len(rows)
    assert set(summary) == {r["status"] for r in rows}
    assert summary["ran_empty"] == 1
    assert summary["not_selected"] == 1
    assert summary["disabled_by_config"] == 1
    assert summary["not_registered"] == 1