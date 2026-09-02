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


# ---------------------------------------------------------------------------
# P3 Batch 3+4 review fixes: MINOR F (word-boundary persona matching) and
# MINOR E (select_persona_framing evaluated once per brief).
# ---------------------------------------------------------------------------


def test_persona_keywords_match_whole_words_only():
    """MINOR F: keyword matches are word-boundary regex, not raw substrings."""
    from src.export.briefs import _persona_bucket

    # 'chief' inside a larger word must not match...
    assert _persona_bucket(Contact(person_key="p9", domain="d.com", title="Subchief of Ops")) is None
    # ...while whole-word matches still do.
    assert _persona_bucket(Contact(person_key="p8", domain="d.com", title="Chief of Staff")) == "exec"
    assert _persona_bucket(Contact(person_key="p7", domain="d.com", title="Chief Revenue Officer")) == "revenue"


def test_render_brief_evaluates_persona_framing_once(monkeypatch):
    """MINOR E: select_persona_framing is bound to a local, called exactly once."""
    import src.export.briefs as briefs_mod

    calls = []
    real = briefs_mod.select_persona_framing

    def counting(contacts, plays):
        calls.append(1)
        return real(contacts, plays)

    monkeypatch.setattr(briefs_mod, "select_persona_framing", counting)
    _render([CRO])
    assert len(calls) == 1
