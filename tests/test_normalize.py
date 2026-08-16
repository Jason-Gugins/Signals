"""Tests for candidate normalization and deterministic signal IDs."""

from __future__ import annotations

from pathlib import Path

from src.core.models import Account
from src.signals.normalize import (
    SignalCandidate,
    make_signal_id,
    normalize_batch,
    normalize_candidate,
)
from src.signals.taxonomy import Taxonomy


TAX = Taxonomy.load(str(Path(__file__).resolve().parents[1] / "config" / "signals.yaml"))
ACCT = Account(domain="acme.com", name="Acme")


def _cand(**kw) -> SignalCandidate:
    data = dict(
        signal_type="funding_round",
        observed_at="2026-08-01",
        natural_key="0001",
        title="Series B",
        confidence=0.8,
    )
    data.update(kw)
    return SignalCandidate(**data)


def test_signal_id_stable_and_changes_with_inputs():
    a = make_signal_id("Acme.com", "Funding_Round", "ABC")
    b = make_signal_id("acme.com", "funding_round", "abc")
    c = make_signal_id("acme.com", "funding_round", "abd")
    assert a == b
    assert a != c
    assert len(a) == 16


def test_taxonomy_fields_copied_candidate_polarity_ignored():
    cand = _cand()
    cand.polarity = "negative"  # type: ignore[attr-defined]
    sig = normalize_candidate(
        cand, account=ACCT, source="sec_edgar", taxonomy=TAX, now="2026-08-16"
    )
    assert sig.category == "financial"
    assert sig.origin == "internal"
    assert sig.catalyst == "primary"
    assert sig.polarity == "positive"
    assert sig.degree == 0
    assert sig.signal_id == make_signal_id("acme.com", "funding_round", "0001")
    assert sig.first_seen_at == "2026-08-16"
    assert sig.last_seen_at == "2026-08-16"
    assert sig.source == "sec_edgar"


def test_missing_observed_at_rejected():
    valid, rejected = normalize_batch(
        [_cand(observed_at="")],
        account=ACCT,
        source="sec_edgar",
        taxonomy=TAX,
        now="2026-08-16",
    )
    assert valid == []
    assert len(rejected) == 1
    assert "observed" in rejected[0][1].lower() or "date" in rejected[0][1].lower()


def test_future_date_beyond_tolerance_rejected():
    valid, rejected = normalize_batch(
        [_cand(observed_at="2026-09-01")],
        account=ACCT,
        source="sec_edgar",
        taxonomy=TAX,
        now="2026-08-16",
    )
    assert valid == []
    assert rejected


def test_unknown_type_rejected():
    valid, rejected = normalize_batch(
        [_cand(signal_type="not_a_real_type")],
        account=ACCT,
        source="sec_edgar",
        taxonomy=TAX,
        now="2026-08-16",
    )
    assert valid == []
    assert rejected[0][1] == "unknown_signal_type"


def test_confidence_clamped():
    sig = normalize_candidate(
        _cand(confidence=1.7),
        account=ACCT,
        source="sec_edgar",
        taxonomy=TAX,
        now="2026-08-16",
    )
    assert sig.confidence == 1.0
    sig2 = normalize_candidate(
        _cand(confidence=-0.2),
        account=ACCT,
        source="sec_edgar",
        taxonomy=TAX,
        now="2026-08-16",
    )
    assert sig2.confidence == 0.0


def test_batch_preserves_order_and_splits():
    cands = [
        _cand(natural_key="ok1", observed_at="2026-08-01"),
        _cand(signal_type="nope", natural_key="bad"),
        _cand(natural_key="ok2", observed_at="2026-07-01"),
    ]
    valid, rejected = normalize_batch(
        cands, account=ACCT, source="news_rss", taxonomy=TAX, now="2026-08-16"
    )
    assert [s.evidence_data.get("natural_key") or s.signal_id for s in valid]
    assert [v.title for v in valid] == ["Series B", "Series B"]
    assert len(valid) == 2
    assert len(rejected) == 1
    assert valid[0].signal_id == make_signal_id("acme.com", "funding_round", "ok1")
    assert valid[1].signal_id == make_signal_id("acme.com", "funding_round", "ok2")
