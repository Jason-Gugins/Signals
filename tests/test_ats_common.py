"""Tests for ATS common helpers and job upsert."""

from __future__ import annotations

import json

from src.core.db import Database
from src.sources.ats.common import JobPost, job_key, parse_location, snapshot_jobs, strip_html, upsert_jobs


def test_job_key_stable():
    assert job_key("greenhouse", "acme", "101") == "greenhouse:acme:101"
    assert job_key("greenhouse", "acme", "101") == job_key("greenhouse", "acme", "101")


def test_parse_location_table():
    cases = {
        "Toronto, ON, Canada": ("Toronto", "Ontario", "Canada", None),
        "Remote - US": (None, None, None, True),
        "San Francisco or Remote": ("San Francisco", "California", "United States", True),
        "London, UK": ("London", None, "United Kingdom"),
        "Austin": ("Austin", "Texas", "United States", None),
        "New York, NY, USA": ("New York", "NY", "United States", None),
        "Seattle, WA": ("Seattle", "Washington", "United States", None),
        "Chicago, IL, United States": ("Chicago", "IL", "United States", None),
        "Remote": (None, None, None, True),
        "Vancouver": ("Vancouver", "British Columbia", "Canada", None),
        "Boston": ("Boston", "Massachusetts", "United States", None),
        "Montreal": ("Montreal", "Quebec", "Canada", None),
        "": (None, None, None, None),
        None: (None, None, None, None),
        "Dublin, Ireland": ("Dublin", None, "Ireland", None),
    }
    for raw, expected in cases.items():
        got = parse_location(raw)
        # allow 3-tuple expected without remote
        if len(expected) == 3:
            assert got[0] == expected[0] and got[2] == expected[2], (raw, got)
        else:
            assert got[0] == expected[0], (raw, got)
            assert got[3] == expected[3], (raw, got)
            if expected[2] is not None:
                assert got[2] == expected[2], (raw, got)


def test_upsert_first_seen_and_closed(tmp_path):
    db = Database(tmp_path / "s.db")
    a = JobPost(external_id="1", title="AE", url="u1", posted_at="2026-08-01")
    b = JobPost(external_id="2", title="Eng", url="u2", posted_at="2026-08-01")
    n, s = upsert_jobs(db, "acme.com", [a, b], "greenhouse", now="2026-08-10", token="acme")
    assert n == 2 and s == 0
    row = db.one("SELECT first_seen_at, last_seen_at FROM jobs WHERE external_id='1'")
    assert row["first_seen_at"] == "2026-08-10"
    n, s = upsert_jobs(db, "acme.com", [a], "greenhouse", now="2026-08-20", token="acme", mark_closed=True)
    assert n == 0 and s == 1
    one = db.one("SELECT first_seen_at, last_seen_at FROM jobs WHERE external_id='1'")
    assert one["first_seen_at"] == "2026-08-10"
    assert one["last_seen_at"] == "2026-08-20"
    two = db.one("SELECT closed_at FROM jobs WHERE external_id='2'")
    assert two["closed_at"] == "2026-08-20"


def test_snapshot_department_counts(tmp_path):
    db = Database(tmp_path / "s.db")
    jobs = [
        JobPost(external_id="1", title="A", url="u", posted_at="2026-01-01", department="Sales", country="Canada"),
        JobPost(external_id="2", title="B", url="u", posted_at="2026-01-01", department="Sales", country="Canada"),
        JobPost(external_id="3", title="C", url="u", posted_at="2026-01-01", department="Eng", country="United States"),
    ]
    upsert_jobs(db, "acme.com", jobs, "greenhouse", now="2026-08-01", token="acme")
    snapshot_jobs(db, "acme.com", as_of="2026-08-01")
    snap = db.one("SELECT * FROM job_snapshots WHERE domain='acme.com'")
    assert snap["open_count"] == 3
    assert json.loads(snap["by_department"])["Sales"] == 2


def test_strip_html():
    assert strip_html("<p>Hello &amp; goodbye</p>") == "Hello & goodbye"
    assert strip_html(None) is None
