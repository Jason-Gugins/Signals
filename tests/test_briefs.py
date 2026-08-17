"""Tests for markdown account briefs."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from src.core.models import Account, Contact, Signal
from src.export.briefs import render_brief, write_brief
from src.signals.plays import PlayAssignment
from src.signals.score import Contribution, ScoreResult
from src.signals.taxonomy import Taxonomy
from src.signals.tier import TierResult


TODAY = date(2026, 8, 16)
TAX = Taxonomy.load("config/signals.yaml")


def test_sections_and_determinism(tmp_path):
    spec = TAX.get("funding_round")
    sig = Signal(
        signal_id="f1",
        domain="acme.com",
        signal_type="funding_round",
        category=spec.category,
        origin=spec.origin,
        catalyst=spec.catalyst,
        polarity=spec.polarity,
        observed_at="2026-08-01",
        source="sec_edgar",
        confidence=0.9,
        evidence_data={"round_stage": "Series B"},
        url="https://sec.gov/x",
        evidence="Raised Series B",
    )
    orphan = Signal(
        signal_id="a1",
        domain="acme.com",
        signal_type="award",
        category="neutral",
        origin="internal",
        catalyst="secondary",
        polarity="neutral",
        observed_at="2026-07-01",
        source="news",
        confidence=0.5,
        title="Best Workplace",
    )
    account = Account(
        domain="acme.com", name="Acme", industry="Software",
        employee_count=200, hq_city="Toronto", hq_country="Canada",
    )
    score = ScoreResult(
        72.0, 40.0,
        [
            Contribution("f1", "funding_round", "sec_edgar", 30, 1, 0.9, 27),
            Contribution("a1", "award", "news", 6, 1, 0.5, 3),
        ],
        [], 0, 1.1,
    )
    tier = TierResult(1, "active", "Tier 1: score 72.")
    plays = [
        PlayAssignment(
            "growth_pitch", "The Growth Pitch", "f1", 1, 7, {},
            "Saw the Series B.", "Hi Jane, ...", "Open?", "Runway.",
        ),
    ]
    contacts = [
        Contact(
            person_key="jane", name="Jane Doe", title="CRO",
            persona="economic_buyer", linkedin_url="https://linkedin.com/in/jane",
        )
    ]
    a = render_brief(account, [sig, orphan], score, tier, plays, contacts, today=TODAY)
    b = render_brief(account, [sig, orphan], score, tier, plays, contacts, today=TODAY)
    assert a == b
    for heading in (
        "# Acme",
        "## Why now (top signals)",
        "## Stacked plays",
        "## Who to contact",
        "## Evidence trail",
        "## Watch items",
    ):
        assert heading in a
    assert "Tier 1" in a and "72" in a and "active" in a
    assert "Saw the Series B." in a
    assert "Jane Doe" in a
    assert "https://sec.gov/x" in a
    assert "_Generated 2026-08-16" in a
    path = write_brief(str(tmp_path / "acme.md"), a)
    raw = Path(path).read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert raw.decode("utf-8") == a


def test_no_contact_and_no_url():
    account = Account(domain="x.com", name="X")
    spec = TAX.get("award")
    sig = Signal(
        signal_id="a", domain="x.com", signal_type="award", category=spec.category,
        origin=spec.origin, catalyst=spec.catalyst, polarity=spec.polarity,
        observed_at="2026-08-01", source="news", confidence=0.5, url=None,
    )
    score = ScoreResult(10, 2, [Contribution("a", "award", "news", 6, 1, 0.5, 2)], [], 0, 1)
    play = PlayAssignment("flattery_opener", "Flattery", "a", 1, 1, {}, "Nice award.", "Hi , ...", "", "")
    text = render_brief(account, [sig], score, TierResult(4, "dormant", "x"), [play], [], today=TODAY)
    assert "No contact identified" in text
    assert "signals deepen --domain x.com" in text
    assert "award" in text
