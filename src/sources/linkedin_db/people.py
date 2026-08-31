"""PURE people-row → contacts and role-change signals."""

from __future__ import annotations

import re
from datetime import date

from src.core.models import Contact
from src.core.textutil import guess_seniority, slugify, stable_id
from src.sources.base import SignalCandidate

_MONTHS = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}


def parse_role_start(dates: str | None) -> date | None:
    s = dates or ""
    m = re.search(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(20\d{2})", s, re.I)
    if m:
        return date(int(m.group(2)), _MONTHS[m.group(1)[:3].lower()], 1)
    m = re.search(r"\b(20\d{2})\s*[–\-]", s)
    if m:
        return date(int(m.group(1)), 1, 1)
    return None


def role_age_months(start: date, as_of: date | None = None) -> int:
    as_of = as_of or date.today()
    return (as_of.year - start.year) * 12 + (as_of.month - start.month)


def people_to_contacts(rows: list[dict], domain: str) -> list[Contact]:
    out = []
    for r in rows:
        slug = r.get("linkedin_slug") or r.get("person_slug")
        key = slug or stable_id(r.get("name") or "", domain)
        out.append(
            Contact(
                person_key=key,
                domain=domain,
                name=r.get("name"),
                title=r.get("job_title") or r.get("title"),
                seniority=guess_seniority(r.get("job_title") or r.get("title")),
                linkedin_slug=slug,
                linkedin_url=r.get("linkedin_url"),
            )
        )
    return out


def detect_role_changes(contacts: list[Contact], people_rows: list[dict], champions: dict, *, today: date, max_role_months: int = 6) -> list[SignalCandidate]:
    by_slug = {c.linkedin_slug: c for c in contacts if c.linkedin_slug}
    out = []
    for r in people_rows:
        slug = r.get("linkedin_slug")
        title = r.get("job_title") or r.get("title") or ""
        start = parse_role_start(r.get("experience_dates") or r.get("dates"))
        lead = guess_seniority(title) in {"c_level", "vp", "director", "head"}
        if start and role_age_months(start, today) <= max_role_months and lead:
            out.append(
                SignalCandidate(
                    signal_type="exec_hire",
                    observed_at=start.isoformat(),
                    natural_key=f"hire:{slug or title}",
                    title=title,
                    confidence=0.8,
                    person_key=slug,
                    evidence_data={"new_role": title, "person_name": r.get("name")},
                )
            )
            champ = champions.get(slug) if slug else None
            if champ and champ.get("prior_domain") and champ.get("prior_domain") != r.get("domain"):
                out.append(
                    SignalCandidate(
                        signal_type="champion_migration",
                        observed_at=start.isoformat(),
                        natural_key=f"champ:{slug}",
                        title=title,
                        confidence=0.95,
                        person_key=slug,
                        evidence_data={
                            "new_role": title,
                            "person_name": r.get("name"),
                            "prior_company": champ.get("prior_company"),
                            "prior_domain": champ.get("prior_domain"),
                        },
                    )
                )
        # promotion: prior role same company lower seniority
        prior_title = r.get("prior_title")
        if prior_title and guess_seniority(title) != "unknown":
            rank = {"unknown": 0, "ic": 1, "manager": 2, "head": 3, "director": 4, "vp": 5, "c_level": 6}
            if rank.get(guess_seniority(title), 0) > rank.get(guess_seniority(prior_title), 0):
                out.append(
                    SignalCandidate(
                        signal_type="promotion",
                        observed_at=today.isoformat(),
                        natural_key=f"promo:{slug or title}",
                        title=title,
                        confidence=0.7,
                        person_key=slug,
                        evidence_data={"new_role": title, "person_name": r.get("name")},
                    )
                )
        if lead and r.get("role_ended"):
            iso, raw = to_iso_or_raw(r.get("role_ended"), today)
            evidence = {"person_name": r.get("name")}
            if iso:
                observed = iso
            else:
                # Unparseable/absent role_ended: keep the candidate, carry the
                # raw string when present; never fabricate today as the
                # departure date (it would misstate recency/decay).
                observed = ""
                if raw:
                    evidence["date_raw"] = raw
            out.append(
                SignalCandidate(
                    signal_type="exec_departure",
                    observed_at=observed,
                    natural_key=f"depart:{slug or title}",
                    title=title,
                    confidence=0.75,
                    person_key=slug,
                    evidence_data=evidence,
                )
            )
    return out


def to_iso_or_raw(value, today: date) -> tuple[str | None, str | None]:
    """Return ``(iso_date, raw)`` for a person-field date.

    Parseable -> ``(iso, None)``. Present-but-unparseable -> ``(None, raw)``.
    Absent -> ``(None, None)``. Never fabricates a today-substitute: callers
    decide whether an unknown date still qualifies (they must not silently
    treat unknown as "just happened").
    """
    from src.core.textutil import to_iso_date
    iso = to_iso_date(value)
    if iso:
        return iso, None
    raw = value.strip() if isinstance(value, str) and value.strip() else None
    return None, raw
