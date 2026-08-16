"""Tests for human-readable evidence rendering."""

from __future__ import annotations

from datetime import date

from src.core.models import Account, Signal
from src.signals.evidence import humanize_age, render_evidence


ACCT = Account(domain="acme.com", name="Acme")
TODAY = date(2026, 8, 16)


def _sig(typ: str, observed: str, **data) -> Signal:
    return Signal(
        signal_id="x",
        domain="acme.com",
        signal_type=typ,
        category="financial",
        origin="internal",
        catalyst="primary",
        polarity="positive",
        observed_at=observed,
        source="test",
        title=data.pop("title", "A title"),
        evidence_data=data,
    )


def test_funding_round_template():
    text = render_evidence(
        _sig("funding_round", "2026-08-13", amount_display="$12.5M", round_stage="Series B"),
        account=ACCT,
        today=TODAY,
    )
    assert "12.5M" in text and "Series B" in text
    assert "3 days ago" in text
    assert len(text) <= 200


def test_champion_migration_template():
    text = render_evidence(
        _sig(
            "champion_migration",
            "2026-08-15",
            person_name="Jane Doe",
            prior_company="Gong",
            new_role="VP Sales",
        ),
        account=ACCT,
        today=TODAY,
    )
    assert "Jane Doe" in text and "Gong" in text and "Acme" in text and "VP Sales" in text


def test_hiring_surge_template():
    text = render_evidence(
        _sig("hiring_surge", "2026-08-01", job_count="14", department="Sales", window_days="30"),
        account=ACCT,
        today=TODAY,
    )
    assert "14" in text and "Sales" in text


def test_renewal_window_template():
    text = render_evidence(
        _sig(
            "renewal_window",
            "2026-08-01",
            competitor="Outreach",
            first_seen_human="Jan 2024",
            renewal_month="2027-01",
        ),
        account=ACCT,
        today=TODAY,
    )
    assert "Outreach" in text and "2027-01" in text


def test_layoff_template():
    text = render_evidence(
        _sig("layoff", "2026-07-01", affected="120", location="Austin", effective_date="2026-08-01"),
        account=ACCT,
        today=TODAY,
    )
    assert "120" in text and "Austin" in text


def test_intent_1st_owned_template():
    text = render_evidence(
        _sig("intent_1st_owned", "2026-08-16", visits="4", page_label="pricing"),
        account=ACCT,
        today=TODAY,
    )
    assert "pricing" in text and "today" in text


def test_tech_migration_mentioned_template():
    text = render_evidence(
        _sig("tech_migration_mentioned", "2026-08-01", from_tech="Salesforce", to_tech="HubSpot"),
        account=ACCT,
        today=TODAY,
    )
    assert "Salesforce" in text and "HubSpot" in text


def test_missing_vars_no_exception_no_braces():
    text = render_evidence(_sig("funding_round", "2026-08-13"), account=ACCT, today=TODAY)
    assert "{" not in text and "}" not in text
    assert text.strip()


def test_fallback_and_length_clamp():
    long_title = "x" * 300
    text = render_evidence(
        _sig("award", "2026-08-01", title=long_title),
        account=ACCT,
        today=TODAY,
    )
    assert len(text) <= 200
    assert "Award" in text or "award" in text.lower() or "x" in text


def test_humanize_age_boundaries():
    today = date(2026, 8, 16)
    assert humanize_age("2026-08-16", today) == "today"
    assert humanize_age("2026-08-15", today) == "yesterday"
    assert humanize_age("2026-08-14", today) == "2 days ago"
    assert humanize_age("2026-07-18", today) == "29 days ago"
    assert humanize_age("2026-07-16", today) == "1 month ago"
    assert humanize_age("2025-08-17", today) == "12 months ago"
    assert humanize_age("2025-07-12", today).startswith("on ")
    assert humanize_age("2025-07-12", today) == "on 2025-07-12"
