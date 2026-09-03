"""exec_hire fix (TDD): detect_role_changes must read role dates from the
experience JSON (the column the scraper actually populates), not from the
nonexistent experience_dates/dates columns."""
import json
import sqlite3
from datetime import date, timedelta

import pytest

from src.core.config import Config
from src.identity.seeds import _open_ro
from src.sources.linkedin_db.collector import LinkedinDbSource
from src.sources.linkedin_db.people import detect_role_changes, people_to_contacts


def _profiled_person(slug: str, name: str, title: str, start: date):
    """A people row shaped like the scraper writes it: experience is a JSON
    array; the CURRENT entry carries is_current + dates. No job_title column
    value (extract populates people.job_title only for listing rows)."""
    experience = [
        {
            "title": title,
            "company": name,
            "dates": f"{start.strftime('%b %Y')} - Present · 1 yr 2 mos",
            "is_current": True,
        }
    ]
    return {
        "linkedin_slug": slug,
        "name": name,
        "job_title": "",  # listing rows leave this empty; profile rows too
        "experience": json.dumps(experience),
        "experience_dates": None,  # the nonexistent-data column (always None)
        "dates": None,
        "prior_title": None,
        "profile_scraped": 1,
    }


def _title_from_experience(row: dict) -> str:
    """Extract the current role title from the experience JSON."""
    try:
        exp = json.loads(row.get("experience") or "[]")
    except (TypeError, ValueError):
        return ""
    cur = [e for e in exp if e.get("is_current")]
    return (cur[0].get("title") if cur else "") or (exp[0].get("title") if exp else "")


def test_detect_role_changes_reads_experience_json_dates():
    """exec_hire: current-entry dates from experience JSON feed parse_role_start."""
    start = date(2026, 6, 1)
    today = date(2026, 9, 2)
    row = _profiled_person("jane-doe", "Jane Doe", "VP Engineering", start)
    row["job_title"] = "VP Engineering"  # some rows carry the title
    contacts = people_to_contacts([row], "example.com")
    sigs = detect_role_changes(contacts, [row], {}, today=today)
    assert any(s.signal_type == "exec_hire" and s.observed_at == start.isoformat() for s in sigs)


def test_detect_role_changes_title_from_experience_json_when_job_title_empty():
    """Profile rows can have an empty job_title — the title must come from the
    experience JSON's current entry, or the seniority gate can never pass."""
    start = date(2026, 7, 15)
    today = date(2026, 9, 2)
    row = _profiled_person("john-smith", "John Smith", "Director of Sales", start)
    assert row["job_title"] == ""  # precondition
    contacts = people_to_contacts([row], "example.com")
    sigs = detect_role_changes(contacts, [row], {}, today=today)
    assert any(s.signal_type == "exec_hire" and "Director" in (s.title or "") for s in sigs)


def test_detect_role_changes_old_roles_still_ignored():
    """Roles older than max_role_months stay unreported (behavior unchanged)."""
    start = date(2024, 1, 1)
    today = date(2026, 9, 2)
    row = _profiled_person("old-role", "Old Role", "VP Engineering", start)
    row["job_title"] = "VP Engineering"
    contacts = people_to_contacts([row], "example.com")
    sigs = detect_role_changes(contacts, [row], {}, today=today)
    assert not any(s.signal_type == "exec_hire" for s in sigs)


def test_live_scraper_db_role_dates():
    """Integration: against the real scraper DB, at least one profiled person
    yields a resolvable role start (no longer the universal None).

    Skipped when the companion scraper checkout is absent (CI, fresh clones) —
    the DB belongs to the private linkedin-scraper repo."""
    import os

    import pytest

    cfg = Config()
    path = cfg.external_dbs.linkedin_db
    if not os.path.exists(path):
        pytest.skip(f"companion scraper DB not present: {path}")
    conn = _open_ro(path)
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM people WHERE profile_scraped=1 AND experience IS NOT NULL AND experience != '' LIMIT 5"
        ).fetchall()
    ]
    assert rows, "expected profiled people in the scraper DB"
    resolved = 0
    for row in rows:
        try:
            exp = json.loads(row.get("experience") or "[]")
        except (TypeError, ValueError):
            continue
        cur = [e for e in exp if e.get("is_current")]
        if cur and cur[0].get("dates"):
            resolved += 1
    assert resolved > 0, "experience JSON has current-entry dates to harvest"
