"""Tests for signal store merge semantics."""

from __future__ import annotations

from pathlib import Path

from src.core.db import Database
from src.core.models import Account, Signal
from src.signals.normalize import make_signal_id
from src.signals.store import SignalStore
from src.signals.taxonomy import Taxonomy


TAX = Taxonomy.load(str(Path(__file__).resolve().parents[1] / "config" / "signals.yaml"))


def _sig(**kw) -> Signal:
    data = dict(
        signal_id=make_signal_id("acme.com", "funding_round", "r1"),
        domain="acme.com",
        signal_type="funding_round",
        category="financial",
        origin="internal",
        catalyst="primary",
        polarity="positive",
        observed_at="2026-06-01",
        source="sec_edgar",
        first_seen_at="2026-08-01",
        last_seen_at="2026-08-01",
        confidence=0.6,
        summary=None,
        url=None,
        evidence=None,
        raw_ref=None,
    )
    data.update(kw)
    return Signal(**data)


def test_first_upsert_new_second_false(tmp_path):
    store = SignalStore(Database(tmp_path / "s.db"), taxonomy=TAX)
    assert store.upsert(_sig()) is True
    assert store.upsert(_sig(last_seen_at="2026-08-10")) is False


def test_merge_first_last_observed(tmp_path):
    store = SignalStore(Database(tmp_path / "s.db"), taxonomy=TAX)
    store.upsert(_sig())
    store.upsert(
        _sig(
            first_seen_at="2026-08-10",
            last_seen_at="2026-08-10",
            observed_at="2026-05-01",
            confidence=0.9,
        )
    )
    got = store.get(_sig().signal_id)
    assert got.first_seen_at == "2026-08-01"
    assert got.last_seen_at == "2026-08-10"
    assert got.observed_at == "2026-05-01"
    assert got.confidence == 0.9


def test_null_summary_fills_non_null_preserved(tmp_path):
    store = SignalStore(Database(tmp_path / "s.db"), taxonomy=TAX)
    store.upsert(_sig())
    store.upsert(_sig(summary="raised series B", url="https://e/x"))
    got = store.get(_sig().signal_id)
    assert got.summary == "raised series B"
    assert got.url == "https://e/x"
    store.upsert(_sig(summary="other", url="https://other"))
    got = store.get(_sig().signal_id)
    assert got.summary == "raised series B"
    assert got.url == "https://e/x"


def test_for_account_filters_and_order(tmp_path):
    store = SignalStore(Database(tmp_path / "s.db"), taxonomy=TAX)
    award = _sig(
        signal_id=make_signal_id("acme.com", "award", "1"),
        signal_type="award",
        category="neutral",
        catalyst="secondary",
        polarity="neutral",
        observed_at="2026-08-01",
        confidence=0.9,
    )
    fund = _sig(
        signal_id=make_signal_id("acme.com", "funding_round", "2"),
        observed_at="2026-08-01",
        confidence=0.5,
    )
    old = _sig(
        signal_id=make_signal_id("acme.com", "layoff", "3"),
        signal_type="layoff",
        category="negative",
        polarity="negative",
        observed_at="2025-01-01",
    )
    store.upsert(award)
    store.upsert(fund)
    store.upsert(old)
    rows = store.for_account("acme.com", since="2026-01-01")
    assert [r.signal_type for r in rows] == ["funding_round", "award"]
    only = store.for_account("acme.com", types=["award"])
    assert [r.signal_type for r in only] == ["award"]
    cats = store.for_account("acme.com", categories=["negative"])
    assert [r.signal_type for r in cats] == ["layoff"]


def test_new_since_primary_only(tmp_path):
    store = SignalStore(Database(tmp_path / "s.db"), taxonomy=TAX)
    store.upsert(
        _sig(
            signal_id=make_signal_id("acme.com", "funding_round", "n"),
            first_seen_at="2026-08-10T00:00:00",
        )
    )
    store.upsert(
        _sig(
            signal_id=make_signal_id("acme.com", "award", "n2"),
            signal_type="award",
            category="neutral",
            catalyst="secondary",
            polarity="neutral",
            first_seen_at="2026-08-10T00:00:00",
        )
    )
    all_new = store.new_since("2026-08-09")
    assert {s.signal_type for s in all_new} == {"funding_round", "award"}
    primary = store.new_since("2026-08-09", primary_only=True)
    assert [s.signal_type for s in primary] == ["funding_round"]


def test_purge_domain_removes_signals_and_plays(tmp_path):
    db = Database(tmp_path / "s.db")
    store = SignalStore(db, taxonomy=TAX)
    sig = _sig()
    store.upsert(sig)
    db.execute(
        "INSERT INTO play_assignments(domain, play_id, signal_id) VALUES (?,?,?)",
        ("acme.com", "growth_pitch", sig.signal_id),
    )
    n = store.purge_domain("acme.com")
    assert n == 1
    assert store.get(sig.signal_id) is None
    assert db.one("SELECT COUNT(*) AS n FROM play_assignments WHERE domain=?", ("acme.com",))["n"] == 0


def test_upsert_many_and_counts(tmp_path):
    store = SignalStore(Database(tmp_path / "s.db"), taxonomy=TAX)
    a = _sig(signal_id=make_signal_id("acme.com", "funding_round", "x"))
    b = _sig(
        signal_id=make_signal_id("acme.com", "layoff", "y"),
        signal_type="layoff",
        category="negative",
        polarity="negative",
    )
    new, updated = store.upsert_many([a, b])
    assert new == 2 and updated == 0
    new, updated = store.upsert_many([a])
    assert new == 0 and updated == 1
    assert store.counts_by_type("acme.com")["funding_round"] == 1
    assert store.newest("acme.com", "funding_round").signal_id == a.signal_id
