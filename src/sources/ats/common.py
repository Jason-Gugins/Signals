"""Shared job posting model and helpers. Parsers stay pure; upsert does I/O."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html import unescape
from typing import Optional

from src.core.db import Database
from src.core.textutil import clean_text, to_iso_date


@dataclass
class JobPost:
    external_id: str
    title: str
    url: str
    posted_at: str | None
    department: str | None = None
    team: str | None = None
    location_raw: str | None = None
    city: str | None = None
    region: str | None = None
    country: str | None = None
    remote: bool | None = None
    employment_type: str | None = None
    description: str | None = None
    comp_min: float | None = None
    comp_max: float | None = None
    comp_currency: str | None = None
    extra: dict = field(default_factory=dict)


COUNTRY_ALIASES = {
    "us": "United States",
    "usa": "United States",
    "united states": "United States",
    "u.s.": "United States",
    "uk": "United Kingdom",
    "u.k.": "United Kingdom",
    "united kingdom": "United Kingdom",
    "ca": "Canada",
    "canada": "Canada",
    "de": "Germany",
    "germany": "Germany",
    "fr": "France",
    "au": "Australia",
    "australia": "Australia",
    "in": "India",
    "india": "India",
    "ie": "Ireland",
    "ireland": "Ireland",
    "nl": "Netherlands",
    "on": "Ontario",
}

MAJOR_CITIES = {
    "toronto": ("Toronto", "Ontario", "Canada"),
    "vancouver": ("Vancouver", "British Columbia", "Canada"),
    "montreal": ("Montreal", "Quebec", "Canada"),
    "san francisco": ("San Francisco", "California", "United States"),
    "new york": ("New York", "New York", "United States"),
    "london": ("London", None, "United Kingdom"),
    "austin": ("Austin", "Texas", "United States"),
    "seattle": ("Seattle", "Washington", "United States"),
    "chicago": ("Chicago", "Illinois", "United States"),
    "boston": ("Boston", "Massachusetts", "United States"),
}


def job_key(source: str, token: str, external_id: str) -> str:
    return f"{source}:{token}:{external_id}"


def strip_html(s: str | None) -> str | None:
    if s is None:
        return None
    text = unescape(re.sub(r"<[^>]+>", " ", s))
    return clean_text(text)


def parse_location(raw: str | None) -> tuple[str | None, str | None, str | None, bool | None]:
    if not raw:
        return None, None, None, None
    s = raw.strip()
    remote = bool(re.search(r"\bremote\b", s, re.I))
    city = region = country = None
    parts = [p.strip() for p in re.split(r"[,/]| or ", s) if p.strip() and not re.fullmatch(r"remote(?:\s*-.*)?", p.strip(), re.I)]
    if not parts:
        return None, None, None, True if remote else None
    # last part may be country
    last = parts[-1]
    last_key = last.casefold().replace(".", "")
    if last_key in COUNTRY_ALIASES and last_key not in {"on"}:
        country = COUNTRY_ALIASES[last_key]
        parts = parts[:-1]
    elif last_key in {"united states", "canada", "united kingdom", "germany", "france", "australia", "india", "ireland", "netherlands"}:
        country = COUNTRY_ALIASES.get(last_key, last)
        parts = parts[:-1]
    if parts:
        city = parts[0]
        if len(parts) >= 2:
            mid = parts[1]
            region = COUNTRY_ALIASES.get(mid.casefold(), mid)
    if country is None and city:
        known = MAJOR_CITIES.get(city.casefold())
        if known:
            city, region, country = known[0], region or known[1], known[2]
    if city and re.search(r"\bremote\b", city, re.I):
        city = None
    return city, region, country, remote or None


def upsert_jobs(
    db: Database,
    domain: str,
    jobs: list[JobPost],
    source: str,
    *,
    now: str,
    token: str = "",
    mark_closed: bool = False,
) -> tuple[int, int]:
    new = seen = 0
    keys = []
    for job in jobs:
        key = job_key(source, token or source, job.external_id)
        keys.append(key)
        existing = db.one("SELECT * FROM jobs WHERE job_key = ?", (key,))
        city, region, country, remote = (
            job.city,
            job.region,
            job.country,
            job.remote,
        )
        if job.location_raw and city is None and country is None:
            city, region, country, remote = parse_location(job.location_raw)
        row = {
            "job_key": key,
            "domain": domain,
            "source": source,
            "external_id": job.external_id,
            "title": job.title,
            "department": job.department,
            "team": job.team,
            "location_raw": job.location_raw,
            "city": city,
            "region": region,
            "country": country,
            "remote": None if remote is None else int(remote),
            "employment_type": job.employment_type,
            "description": job.description,
            "url": job.url,
            "posted_at": job.posted_at,
            "last_seen_at": now,
            "comp_min": job.comp_min,
            "comp_max": job.comp_max,
            "comp_currency": job.comp_currency,
            "extra_data": json.dumps(job.extra) if job.extra else None,
        }
        if existing:
            db.upsert("jobs", row, pk="job_key", overwrite={"last_seen_at"})
            seen += 1
        else:
            row["first_seen_at"] = now
            db.upsert("jobs", row, pk="job_key")
            new += 1
    if mark_closed and keys:
        placeholders = ",".join("?" * len(keys))
        db.execute(
            f"""UPDATE jobs SET closed_at = ?
                WHERE domain = ? AND source = ? AND closed_at IS NULL
                  AND job_key NOT IN ({placeholders})""",
            [now, domain, source, *keys],
        )
    elif mark_closed and not keys:
        db.execute(
            "UPDATE jobs SET closed_at = ? WHERE domain = ? AND source = ? AND closed_at IS NULL",
            (now, domain, source),
        )
    return new, seen


def snapshot_jobs(db: Database, domain: str, *, as_of: str) -> None:
    rows = db.query(
        "SELECT department, country FROM jobs WHERE domain = ? AND closed_at IS NULL",
        (domain,),
    )
    by_dept: dict[str, int] = {}
    by_cty: dict[str, int] = {}
    for r in rows:
        d = r["department"] or "unknown"
        c = r["country"] or "unknown"
        by_dept[d] = by_dept.get(d, 0) + 1
        by_cty[c] = by_cty.get(c, 0) + 1
    db.upsert(
        "job_snapshots",
        {
            "domain": domain,
            "as_of": as_of,
            "open_count": len(rows),
            "by_department": json.dumps(by_dept),
            "by_country": json.dumps(by_cty),
        },
        pk=("domain", "as_of"),
        overwrite={"open_count", "by_department", "by_country"},
    )
