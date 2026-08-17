"""Derive hiring / geo / migration signals from a jobs window. PURE."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from src.core.textutil import guess_seniority, to_iso_date
from src.sources.base import SignalCandidate


@dataclass
class JobsWindow:
    domain: str
    jobs: list[dict]
    prior_countries: set[str]
    prior_departments: set[str]
    baseline_open_by_dept: dict[str, int]


MIGRATION_PATTERNS = [
    re.compile(
        r"migrat\w+\s+(?:from|off(?:\s+of)?)\s+(?P<from>[A-Z][\w .+-]{2,30})\s+to\s+(?P<to>[A-Z][\w .+-]{2,30})",
        re.I,
    ),
    re.compile(
        r"replac\w+\s+(?P<from>[A-Z][\w .+-]{2,30})\s+with\s+(?P<to>[A-Z][\w .+-]{2,30})",
        re.I,
    ),
    re.compile(r"sunset\w*\s+(?P<from>[A-Z][\w .+-]{2,30})", re.I),
    re.compile(
        r"consolidat\w+\s+.{0,40}\bonto\s+(?P<to>[A-Z][\w .+-]{2,30})",
        re.I,
    ),
]

PROJECT_PATTERNS = [
    re.compile(r"\b(?:we are|we're)\s+(?:building|standing up|rolling out)\s+(?P<what>[A-Za-z][\w .+-]{2,40})", re.I),
]

_LEAD = {"c_level", "vp", "director", "head"}


def _cite(jobs: list[dict]) -> dict:
    titles = [j.get("title") for j in jobs if j.get("title")][:3]
    urls = [j.get("url") for j in jobs if j.get("url")][:3]
    return {"job_titles": titles, "job_urls": urls}


def analyze_jobs(w: JobsWindow, *, today: date, cfg: dict) -> list[SignalCandidate]:
    surge_min = int(cfg.get("surge_min_roles", 5))
    surge_days = int(cfg.get("surge_window_days", 60))
    iso_week = f"{today.isocalendar()[0]}-W{today.isocalendar()[1]:02d}"
    iso_month = today.strftime("%Y-%m")
    cutoff_surge = today - timedelta(days=surge_days)
    cutoff_office = today - timedelta(days=90)
    out: list[SignalCandidate] = []

    open_jobs = [j for j in w.jobs if not j.get("closed_at")]
    by_dept: dict[str, list[dict]] = defaultdict(list)
    by_country: dict[str, list[dict]] = defaultdict(list)
    by_city: dict[str, list[dict]] = defaultdict(list)
    for j in open_jobs:
        if j.get("department"):
            by_dept[j["department"]].append(j)
        if j.get("country"):
            by_country[j["country"]].append(j)
        if j.get("city"):
            posted = to_iso_date(j.get("posted_at"))
            if posted and date.fromisoformat(posted) >= cutoff_office:
                by_city[j["city"]].append(j)

    # hiring_surge
    for dept, rows in by_dept.items():
        recent = []
        for j in rows:
            posted = to_iso_date(j.get("posted_at"))
            if posted and date.fromisoformat(posted) >= cutoff_surge:
                recent.append(j)
        baseline = w.baseline_open_by_dept.get(dept, 0)
        ratio_hit = baseline > 0 and len(rows) >= 1.5 * baseline and (len(rows) - baseline) >= 4
        count_hit = len(recent) >= surge_min
        if count_hit or ratio_hit:
            out.append(
                SignalCandidate(
                    signal_type="hiring_surge",
                    observed_at=today.isoformat(),
                    natural_key=f"surge:{dept}:{iso_week}",
                    title=f"{dept} hiring surge",
                    confidence=0.85,
                    evidence_data={**_cite(recent or rows), "department": dept, "job_count": len(recent or rows)},
                )
            )

    # leadership
    for j in open_jobs:
        if guess_seniority(j.get("title")) in _LEAD:
            out.append(
                SignalCandidate(
                    signal_type="leadership_job_open",
                    observed_at=to_iso_date(j.get("posted_at")) or today.isoformat(),
                    natural_key=f"job:{j.get('job_key') or j.get('external_id')}",
                    title=j.get("title"),
                    url=j.get("url"),
                    confidence=0.9,
                    evidence_data={**_cite([j]), "department": j.get("department")},
                )
            )

    # department expansion
    for dept, rows in by_dept.items():
        if len(rows) >= 2 and dept not in w.prior_departments:
            out.append(
                SignalCandidate(
                    signal_type="department_expansion",
                    observed_at=today.isoformat(),
                    natural_key=f"deptnew:{dept}:{iso_month}",
                    title=f"New {dept} department",
                    confidence=0.75,
                    evidence_data={**_cite(rows), "department": dept},
                )
            )

    # new geo
    for country, rows in by_country.items():
        if country not in w.prior_countries:
            out.append(
                SignalCandidate(
                    signal_type="new_geo",
                    observed_at=today.isoformat(),
                    natural_key=f"geo:{country}:{iso_month}",
                    title=f"Hiring in {country}",
                    confidence=0.8,
                    evidence_data={**_cite(rows), "country": country},
                )
            )

    # office open
    for city, rows in by_city.items():
        if len(rows) >= 3:
            out.append(
                SignalCandidate(
                    signal_type="office_open",
                    observed_at=today.isoformat(),
                    natural_key=f"office:{city}:{iso_month}",
                    title=f"Office activity in {city}",
                    confidence=0.7,
                    evidence_data={**_cite(rows), "city": city},
                )
            )

    # migration + scoop
    for j in open_jobs:
        desc = j.get("description") or ""
        for pat in MIGRATION_PATTERNS:
            m = pat.search(desc)
            if not m:
                continue
            gd = m.groupdict()
            frm, to = (gd.get("from") or "").strip(), (gd.get("to") or "").strip()
            out.append(
                SignalCandidate(
                    signal_type="tech_migration_mentioned",
                    observed_at=to_iso_date(j.get("posted_at")) or today.isoformat(),
                    natural_key=f"mig:{j.get('job_key') or j.get('external_id')}:{frm}:{to}",
                    title=j.get("title"),
                    url=j.get("url"),
                    confidence=0.7,
                    evidence_data={**_cite([j]), "from_tech": frm, "to_tech": to},
                )
            )
            break
        for pat in PROJECT_PATTERNS:
            if pat.search(desc):
                out.append(
                    SignalCandidate(
                        signal_type="internal_project_scoop",
                        observed_at=to_iso_date(j.get("posted_at")) or today.isoformat(),
                        natural_key=f"scoop:{j.get('job_key') or j.get('external_id')}",
                        title=j.get("title"),
                        url=j.get("url"),
                        confidence=0.6,
                        evidence_data=_cite([j]),
                    )
                )
                break

    out.sort(key=lambda c: (c.signal_type, c.natural_key))
    return out
