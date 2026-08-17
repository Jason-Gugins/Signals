"""Tests for play-aware contact selection."""

from __future__ import annotations

from src.core.models import Contact, Signal
from src.signals.contacts import best_contact, email_guess, rank_contacts


def C(name, *, persona="unknown", seniority="ic", department=None, title=None, key=None, **kw):
    return Contact(
        person_key=key or name.lower().replace(" ", "-"),
        name=name,
        persona=persona,
        seniority=seniority,
        department=department,
        title=title or name,
        **kw,
    )


def test_persona_beats_seniority():
    ceo = C("Pat CEO", persona="economic_buyer", seniority="c_level")
    champ = C("Sam Champ", persona="champion", seniority="manager")
    ranked = rank_contacts([ceo, champ], "relationship_pitch", None)
    assert ranked[0].name == "Sam Champ"


def test_department_hint():
    sales = C("Sales Head", persona="economic_buyer", seniority="head", department="Sales")
    eng = C("Eng Head", persona="economic_buyer", seniority="c_level", department="Engineering")
    ranked = rank_contacts([eng, sales], "scaling_pitch", None, department_hint="Sales")
    assert ranked[0].department == "Sales"


def test_fresh_eyes_targets_named_exec():
    newbie = C("Jane Doe", persona="economic_buyer", seniority="vp", key="jane-doe")
    other = C("Old Guard", persona="economic_buyer", seniority="c_level", key="old")
    sig = Signal(
        signal_id="h",
        domain="acme.com",
        signal_type="exec_hire",
        category="hiring",
        origin="internal",
        catalyst="primary",
        polarity="positive",
        observed_at="2026-08-01",
        source="t",
        person_key="jane-doe",
        evidence_data={"person_name": "Jane Doe", "new_role": "VP Sales"},
    )
    assert best_contact([other, newbie], "fresh_eyes", sig).person_key == "jane-doe"


def test_tiebreak_stable():
    a = C("Ann", persona="champion", seniority="director")
    b = C("Bob", persona="champion", seniority="director")
    r1 = [c.person_key for c in rank_contacts([b, a], "relationship_pitch", None)]
    r2 = [c.person_key for c in rank_contacts([a, b], "relationship_pitch", None)]
    assert r1 == r2


def test_email_guess():
    c = C("Jane Doe", email_pattern=None)
    assert email_guess(c, "acme.com", None) is None
    assert email_guess(c, "acme.com", "first.last") == "jane.doe@acme.com"
    c2 = C("Jane Doe", email_pattern="first.last")
    assert email_guess(c2, "acme.com", None) == "jane.doe@acme.com"
