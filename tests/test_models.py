"""Tests for dataclass models and DB row round-trips."""

from __future__ import annotations

import pytest

from src.core.models import Account, Contact, Document, ScoreComponents, Signal


def test_account_round_trip():
    acct = Account(
        domain="acme.com",
        name="Acme",
        icp_fit=1.25,
        icp_reasons=["Headcount in ICP band"],
        extra_data={"foo": 1},
        disqualified=True,
        disqualify_reason="Competitor",
    )
    row = acct.to_db_row()
    assert row["disqualified"] == 1
    assert isinstance(row["icp_reasons"], str)
    back = Account.from_db_row(row)
    assert back == acct


def test_contact_round_trip():
    c = Contact(
        person_key="jane-doe",
        domain="acme.com",
        name="Jane Doe",
        is_champion=True,
        extra_data={"src": "champions.csv"},
    )
    back = Contact.from_db_row(c.to_db_row())
    assert back == c
    assert back.is_champion is True


def test_signal_round_trip():
    sig = Signal(
        signal_id="abc123",
        domain="acme.com",
        signal_type="funding_round",
        category="financial",
        origin="internal",
        catalyst="primary",
        polarity="positive",
        observed_at="2026-08-01",
        source="sec_edgar",
        evidence_data={"round_stage": "Series B"},
        first_seen_at="2026-08-02",
        last_seen_at="2026-08-02",
    )
    back = Signal.from_db_row(sig.to_db_row())
    assert back == sig


def test_document_round_trip_excludes_body():
    doc = Document(
        doc_id="deadbeef",
        source="sec_edgar",
        url="https://example.com/x",
        status=200,
        body=b"hello\x00",
    )
    row = doc.to_db_row()
    assert "body" not in row
    back = Document.from_db_row(row)
    assert back.doc_id == "deadbeef"
    assert back.source == "sec_edgar"
    assert back.body is None


def test_score_components_round_trip():
    sc = ScoreComponents(
        per_signal=[{"signal_id": "x", "points": 10.0}],
        combos=[{"id": "super_signal", "bonus": 25}],
        raw=35.0,
        score=58.4,
    )
    back = ScoreComponents.from_db_row(sc.to_db_row())
    assert back == sc


def test_empty_json_fields_become_none_in_row():
    acct = Account(domain="acme.com")
    row = acct.to_db_row()
    assert row["extra_data"] is None
    assert row["icp_reasons"] is None

    sig = Signal(
        signal_id="x",
        domain="acme.com",
        signal_type="award",
        category="neutral",
        origin="internal",
        catalyst="secondary",
        polarity="neutral",
        observed_at="2026-01-01",
        source="news_rss",
    )
    assert sig.to_db_row()["evidence_data"] is None


def test_unknown_columns_dropped():
    row = {
        "domain": "acme.com",
        "name": "Acme",
        "not_a_column": "nope",
        "icp_reasons": None,
        "extra_data": None,
    }
    acct = Account.from_db_row(row)
    assert acct.domain == "acme.com"
    assert acct.name == "Acme"
    assert not hasattr(acct, "not_a_column")


def test_signal_requires_observed_at():
    with pytest.raises(TypeError):
        Signal(
            signal_id="x",
            domain="acme.com",
            signal_type="award",
            category="neutral",
            origin="internal",
            catalyst="secondary",
            polarity="neutral",
            source="news_rss",
        )


def test_account_icp_fit_defaults_to_one():
    assert Account(domain="acme.com").icp_fit == 1.0
