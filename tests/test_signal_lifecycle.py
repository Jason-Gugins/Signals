"""Tests for per-signal-type superseded-signal expiry (soft filter at scoring)."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import yaml

from src.core.models import Account, Signal
from src.signals.lifecycle import is_superseded, partition_signals
from src.signals.taxonomy import Taxonomy

TODAY = date(2026, 9, 1)
TAX = Taxonomy.load(str(Path("config/signals.yaml")))
ACCT = Account(domain="acme.com", name="Acme", icp_fit=1.0)

# per the plan examples (config/signals.yaml): layoff 90, product_launch 60
SUPERSEDE = {"layoff": 90, "product_launch": 60}


def _sig(typ, observed, *, sid=None, conf=1.0, observed_raw=None) -> Signal:
    spec = TAX.get(typ)
    return Signal(
        signal_id=sid or f"{typ}-{observed}",
        domain="acme.com",
        signal_type=typ,
        category=spec.category,
        origin=spec.origin,
        catalyst=spec.catalyst,
        polarity=spec.polarity,
        observed_at=observed,
        source="t",
        confidence=conf,
    )


# ---------- is_superseded table ----------


def test_fresh_signal_not_superseded():
    sig = _sig("layoff", (TODAY - timedelta(days=10)).isoformat())
    assert is_superseded(sig, today=TODAY, supersede_days_by_type=SUPERSEDE) is False


def test_exact_window_boundary_not_superseded():
    # (today - observed).days == supersede_days -> not yet expired (strictly >)
    sig = _sig("layoff", (TODAY - timedelta(days=90)).isoformat())
    assert is_superseded(sig, today=TODAY, supersede_days_by_type=SUPERSEDE) is False


def test_past_window_superseded():
    sig = _sig("layoff", (TODAY - timedelta(days=91)).isoformat())
    assert is_superseded(sig, today=TODAY, supersede_days_by_type=SUPERSEDE) is True


def test_type_without_supersede_days_never_expires():
    sig = _sig("award", "2010-01-01")  # award has no supersede_days
    assert is_superseded(sig, today=TODAY, supersede_days_by_type=SUPERSEDE) is False


def test_unparseable_observed_at_never_expires():
    sig = _sig("layoff", "not-a-date")
    assert is_superseded(sig, today=TODAY, supersede_days_by_type=SUPERSEDE) is False


def test_missing_observed_at_never_expires():
    sig = _sig("layoff", "")
    assert is_superseded(sig, today=TODAY, supersede_days_by_type=SUPERSEDE) is False


def test_unknown_type_never_expires():
    sig = _sig("layoff", "2020-01-01")
    sig.signal_type = "brand_new_type"
    assert is_superseded(sig, today=TODAY, supersede_days_by_type=SUPERSEDE) is False


def test_observed_at_with_timestamp_parses():
    sig = _sig("product_launch", "2026-01-01T00:00:00+00:00")
    assert is_superseded(sig, today=TODAY, supersede_days_by_type=SUPERSEDE) is True


# ---------- partition_signals ----------


def test_partition_signals_splits_active_and_expired():
    fresh = _sig("layoff", (TODAY - timedelta(days=5)).isoformat(), sid="fresh")
    stale = _sig("layoff", (TODAY - timedelta(days=200)).isoformat(), sid="stale")
    forever = _sig("award", "2010-01-01", sid="forever")
    active, expired = partition_signals(
        [stale, fresh, forever], today=TODAY, supersede_days_by_type=SUPERSEDE
    )
    assert [s.signal_id for s in active] == ["fresh", "forever"]
    assert [s.signal_id for s in expired] == ["stale"]


def test_partition_signals_empty():
    assert partition_signals([], today=TODAY, supersede_days_by_type=SUPERSEDE) == ([], [])


# ---------- integration: scoring path soft-filters ----------


def _make_orch(tmp_path):
    from src.core.config import Config
    from src.pipeline.orchestrator import Orchestrator

    cfg = Config()
    cfg.storage.db_path = str(tmp_path / "s.db")
    cfg.storage.raw_dir = str(tmp_path / "raw")
    cfg.storage.briefs_dir = str(tmp_path / "briefs")
    cfg.storage.export_dir = str(tmp_path / "exports")
    cfg.config_dir = "config"
    return Orchestrator(cfg)


def test_expired_signal_excluded_from_score_but_kept_in_db(tmp_path):
    orch = _make_orch(tmp_path)
    csv_path = tmp_path / "seed.csv"
    csv_path.write_text("domain,name\nacme.com,Acme\n", encoding="utf-8")
    orch.seed(csv=str(csv_path), linkedin=False, repvue=False, cohort="t")

    stale_obs = (TODAY - timedelta(days=200)).isoformat()
    fresh_obs = (TODAY - timedelta(days=5)).isoformat()
    orch.signal_store.upsert(_sig("layoff", stale_obs, sid="stale-1"))
    orch.signal_store.upsert(_sig("layoff", fresh_obs, sid="fresh-1"))

    from src.pipeline import orchestrator as orch_mod

    orch_mod.set_today(TODAY)
    try:
        orch.score()
    finally:
        orch_mod.set_today(None)

    # expired signal stays in the DB untouched
    assert orch.signal_store.get("stale-1") is not None
    assert orch.signal_store.get("fresh-1") is not None
    rows = orch.signal_store.for_account("acme.com")
    assert {s.signal_id for s in rows} == {"stale-1", "fresh-1"}

    # scoring only saw the active signal: score contribution from stale-1 absent
    score_rows = orch.db.query("SELECT * FROM score_history WHERE domain = 'acme.com'")
    assert len(score_rows) == 1
    import json

    components = json.loads(score_rows[0]["components"])
    contributing = {c["signal_id"] for c in components.get("contributions", []) if not c.get("dropped_reason")}
    assert "fresh-1" in contributing
    assert "stale-1" not in contributing
