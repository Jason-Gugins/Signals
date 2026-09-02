"""Task 13: persona-variant briefs — select_persona_framing + render_brief wiring."""

from __future__ import annotations

from datetime import date

from src.core.models import Account, Contact
from src.export.briefs import render_brief, select_persona_framing
from src.signals.plays import PlayAssignment
from src.signals.score import ScoreResult
from src.signals.taxonomy import Taxonomy
from src.signals.tier import TierResult

TODAY = date(2026, 9, 2)
TAX = Taxonomy.load("config/signals.yaml")

CRO = Contact(person_key="p1", domain="acme.com", name="Rana", title="Chief Revenue Officer")
NO_TITLE = Contact(person_key="p2", domain="acme.com", name="Sam")
CTO = Contact(person_key="p3", domain="acme.com", name="Lin", title="VP Engineering")


def _render(contacts):
    account = Account(domain="acme.com", name="Acme", industry="Software")
    score = ScoreResult(72.0, 40.0, [], [], 0, 1.0)
    tier = TierResult(2, "opening", "Tier 2: score 72.0.")
    return render_brief(account, [], score, tier, [], contacts, today=TODAY)


def test_select_persona_framing_revenue():
    framing = select_persona_framing([CRO], [])
    assert framing
    assert "revenue" in framing.lower()


def test_select_persona_framing_tech():
    framing = select_persona_framing([CTO], [])
    assert framing
    assert "engineering" in framing.lower()


def test_select_persona_framing_no_persona():
    assert select_persona_framing([NO_TITLE], []) is None
    assert select_persona_framing([], []) is None
    assert select_persona_framing(None, []) is None


def test_cro_brief_has_revenue_framing():
    text = _render([CRO])
    assert "revenue" in text.lower()


def test_no_contact_brief_unchanged():
    text = _render([])
    assert "Framing" not in text
    assert "framing" not in text.lower()


def test_no_persona_contact_brief_unchanged():
    text = _render([NO_TITLE])
    assert "Framing" not in text
