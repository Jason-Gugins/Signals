import pytest
from src.sources.needs.extract import extract_need_statements


def test_extracts_verbatim_operational_sentence():
    text = "Quarterly update. We are consolidating our data systems this year. Thank you."
    rows = extract_need_statements(text)
    assert rows == [
        {
            "quote": "We are consolidating our data systems this year.",
            "matched_phrase": "we are consolidating",
        }
    ]


def test_does_not_promote_generic_marketing_copy():
    assert extract_need_statements("Our platform helps teams consolidate data.") == []
    assert extract_need_statements("Consolidation is a common challenge for data teams.") == []


def test_does_not_return_surrounding_document():
    rows = extract_need_statements("Intro paragraph here. We're migrating billing systems. Footer contact us.")
    assert rows[0]["quote"] == "We're migrating billing systems."
    assert "Intro" not in rows[0]["quote"]
    assert "Footer" not in rows[0]["quote"]


def test_multiple_needs_are_returned_in_document_order():
    text = (
        "We are standing up a new data platform. "
        "Later, we're hiring for a head of revenue operations."
    )
    rows = extract_need_statements(text)
    assert [r["matched_phrase"] for r in rows] == [
        "we are standing up",
        "we're hiring for",
    ]


def test_curly_apostrophe_matches_but_quote_stays_verbatim():
    text = "We\u2019re migrating to a new ledger system."
    rows = extract_need_statements(text)
    assert len(rows) == 1
    assert rows[0]["matched_phrase"] == "we're migrating"
    assert rows[0]["quote"] == text