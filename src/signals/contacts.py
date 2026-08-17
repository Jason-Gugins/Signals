"""Play-aware contact ranking and conservative email inference."""

from __future__ import annotations

from typing import Optional

from src.core.models import Contact, Signal
from src.core.textutil import guess_persona


PLAY_PERSONA_PREFERENCE: dict[str, list[str]] = {
    "growth_pitch": ["economic_buyer", "champion"],
    "roi_pitch": ["economic_buyer", "champion"],
    "relationship_pitch": ["champion"],
    "ecosystem_pitch": ["technical", "champion"],
    "insider_pitch": ["technical", "champion"],
    "scaling_pitch": ["economic_buyer", "champion"],
    "fresh_eyes": ["economic_buyer", "champion"],
}

_SENIORITY = {"c_level": 6, "vp": 5, "director": 4, "head": 3, "manager": 2, "ic": 1, "unknown": 0}


def _persona(c: Contact) -> str:
    return c.persona or guess_persona(c.title) or "unknown"


def rank_contacts(
    contacts: list[Contact],
    play_id: str,
    signal: Signal | None,
    *,
    department_hint: str | None = None,
) -> list[Contact]:
    pref = PLAY_PERSONA_PREFERENCE.get(play_id, [])
    named = None
    if play_id == "fresh_eyes" and signal:
        named = signal.person_key or (signal.evidence_data or {}).get("person_name")

    def key(c: Contact):
        if named:
            hit = c.person_key == named or (c.name or "") == named
            name_rank = 0 if hit else 1
        else:
            name_rank = 0
        persona = _persona(c)
        try:
            p_rank = pref.index(persona)
        except ValueError:
            p_rank = len(pref) + 1
        dept_rank = 0 if (department_hint and c.department == department_hint) else 1
        sen = -_SENIORITY.get(c.seniority or "unknown", 0)
        recency = c.role_started_at or ""
        return (name_rank, p_rank, dept_rank, sen, recency, (c.name or "").casefold())

    return sorted(contacts, key=key)


def best_contact(contacts, play_id, signal, **kw) -> Optional[Contact]:
    ranked = rank_contacts(contacts, play_id, signal, **kw)
    return ranked[0] if ranked else None


def email_guess(contact: Contact, domain: str, pattern: str | None) -> Optional[str]:
    pat = pattern or contact.email_pattern
    if not pat:
        return None
    parts = (contact.name or "").split()
    if not parts:
        return None
    first = parts[0].casefold()
    last = parts[-1].casefold() if len(parts) > 1 else ""
    local = pat
    local = local.replace("first.last", f"{first}.{last}" if last else first)
    local = local.replace("first", first).replace("last", last or first)
    return f"{local}@{domain}"
