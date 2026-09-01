"""Tests for P2 Task 13 — new ATS adapters (rippling/jobvite/breezy/teamtailor)
and the generic /careers HTML fallback.

Vendor fixtures are hand-built to each parser's documented shape, except
breezy whose fixture is the live-probe capture (euler.breezy.hr/json, 200,
2026-08-31 — see data/probe/breezy_probe.json and the breezy.py docstring).
"""

from pathlib import Path

from src.core.models import Account
from src.sources.ats.breezy import BreezySource, parse_breezy
from src.sources.ats.careers_page import CareersPageSource, parse_careers_page
from src.sources.ats.jobvite import JobviteSource, parse_jobvite
from src.sources.ats.rippling import RipplingSource, parse_rippling
from src.sources.ats.teamtailor import TeamtailorSource, parse_teamtailor

FIX = Path("tests/fixtures/ats")


# ── rippling ──────────────────────────────────────────────────────────────

def test_parse_rippling_basic():
    jobs = parse_rippling((FIX / "rippling_jobs.json").read_bytes())
    assert len(jobs) == 2
    j = next(x for x in jobs if x.external_id == "r1")
    assert j.title == "Account Executive"
    assert j.department == "Sales"
    assert j.posted_at == "2026-08-20"
    assert j.location_raw == "New York, NY"
    assert j.remote is not True
    assert j.comp_min == 90000 and j.comp_max == 130000


def test_parse_rippling_string_location_and_aliases():
    jobs = parse_rippling((FIX / "rippling_jobs.json").read_bytes())
    j = next(x for x in jobs if x.external_id == "r2")
    assert j.department == "Engineering"  # team alias
    assert j.remote is True
    assert j.posted_at == "2026-08-25"  # created_at alias
    assert "Build APIs" in (j.description or "")


def test_rippling_source_plan_and_harvest():
    src = RipplingSource()
    acct = Account(domain="acme.com", name="Acme", ats_token="acme")
    tasks = src.plan(acct, None)
    assert len(tasks) == 1
    assert tasks[0].url == "https://jobs.rippling.com/acme/jobs"
    assert tasks[0].meta["vendor"] == "rippling"
    assert src.key == "ats_rippling"
    assert src.requires == ("ats_token",)
    jobs = src.harvest_jobs(type("D", (), {"body": (FIX / "rippling_jobs.json").read_bytes()})(), acct, tasks[0].meta)
    assert len(jobs) == 2
    assert src.parse(type("D", (), {"body": b""})(), acct, {}) == []


# ── jobvite ───────────────────────────────────────────────────────────────

def test_parse_jobvite_basic():
    jobs = parse_jobvite((FIX / "jobvite_jobs.json").read_bytes())
    assert len(jobs) == 2
    j = next(x for x in jobs if x.external_id == "jv1")
    assert j.title == "Sales Development Rep"
    assert j.posted_at == "2026-08-18"  # publishedDate
    assert j.department == "Sales"  # category
    assert j.employment_type == "Full-Time"


def test_parse_jobvite_camelcase_aliases():
    jobs = parse_jobvite((FIX / "jobvite_jobs.json").read_bytes())
    j = next(x for x in jobs if x.external_id == "jv2")
    assert j.title == "Platform Engineer"  # name alias
    assert j.url == "https://jobs.jobvite.com/acme/jobs/jv2"  # jobUrl alias
    assert j.employment_type == "Contract"  # jobType alias
    assert "Ship infra" in (j.description or "")


def test_jobvite_source_plan_and_harvest():
    src = JobviteSource()
    acct = Account(domain="acme.com", name="Acme", ats_token="acme")
    tasks = src.plan(acct, None)
    assert tasks[0].url == "https://jobs.jobvite.com/acme/jobs"
    assert src.key == "ats_jobvite"
    jobs = src.harvest_jobs(type("D", (), {"body": (FIX / "jobvite_jobs.json").read_bytes()})(), acct, tasks[0].meta)
    assert len(jobs) == 2


# ── breezy (live-probed shape) ────────────────────────────────────────────

def test_parse_breezy_live_shape():
    jobs = parse_breezy((FIX / "breezy_jobs.json").read_bytes())
    assert len(jobs) >= 10
    j = jobs[0]
    assert j.external_id and j.title
    assert j.url.startswith("http")
    # published_date is ISO-8601 with millis — to_iso_date normalizes
    assert j.posted_at is None or len(j.posted_at) == 10


def test_parse_breezy_location_country_fallback():
    jobs = parse_breezy((FIX / "breezy_jobs.json").read_bytes())
    # at least one job must carry a location_raw (name or country name)
    assert any(j.location_raw for j in jobs)


def test_breezy_source_plan_and_harvest():
    src = BreezySource()
    acct = Account(domain="acme.com", name="Acme", ats_token="euler")
    tasks = src.plan(acct, None)
    assert tasks[0].url == "https://euler.breezy.hr/json"
    assert src.key == "ats_breezy"
    jobs = src.harvest_jobs(type("D", (), {"body": (FIX / "breezy_jobs.json").read_bytes()})(), acct, tasks[0].meta)
    assert len(jobs) >= 10


# ── teamtailor ────────────────────────────────────────────────────────────

def test_parse_teamtailor_basic():
    jobs = parse_teamtailor((FIX / "teamtailor_jobs.json").read_bytes())
    assert len(jobs) == 2
    j = next(x for x in jobs if x.external_id == "tt1")
    assert j.title == "Customer Success Manager"
    assert j.department == "CS"  # dict department -> name
    assert j.posted_at == "2026-08-19"
    assert j.location_raw == "Chicago, IL"


def test_parse_teamtailor_attributes_shape():
    jobs = parse_teamtailor((FIX / "teamtailor_jobs.json").read_bytes())
    j = next(x for x in jobs if x.external_id == "tt2")
    assert j.title == "Data Engineer"  # from attributes dict
    assert j.remote is True
    assert j.country == "Germany"
    assert j.employment_type == "permanent"  # contract_type alias


def test_teamtailor_source_plan_and_harvest():
    src = TeamtailorSource()
    acct = Account(domain="acme.com", name="Acme", ats_token="acme")
    tasks = src.plan(acct, None)
    assert tasks[0].url == "https://acme.teamtailor.com/jobs.json"
    assert src.key == "ats_teamtailor"
    jobs = src.harvest_jobs(type("D", (), {"body": (FIX / "teamtailor_jobs.json").read_bytes()})(), acct, tasks[0].meta)
    assert len(jobs) == 2


# ── careers-page fallback ─────────────────────────────────────────────────

def test_parse_careers_page_extracts_job_links():
    html = (FIX / "careers_page.html").read_text(encoding="utf-8")
    jobs = parse_careers_page(html, "https://acme.com/careers")
    slugs = {j.external_id for j in jobs}
    assert "senior-account-executive" in slugs
    assert "staff-engineer" in slugs
    assert "support-specialist" in slugs  # query string stripped from slug
    assert "gtm-ops" in slugs  # /join-us/ pattern
    titles = {j.title for j in jobs}
    assert "Senior Account Executive" in titles


def test_parse_careers_page_ignores_non_jobs_and_offsite():
    html = (FIX / "careers_page.html").read_text(encoding="utf-8")
    jobs = parse_careers_page(html, "https://acme.com/careers")
    urls = {j.url for j in jobs}
    assert all(u.startswith("https://acme.com") for u in urls)  # offsite dropped
    assert len(jobs) == 4  # nav/mailto/# excluded
    assert all(j.posted_at is None for j in jobs)


def test_parse_careers_page_dedup_by_slug():
    html = ('<a href="/careers/dup-role">One</a> <a href="/careers/dup-role?x=1">Two</a>'
            '<a href="/careers/dup-role">Three</a>')
    jobs = parse_careers_page(html, "https://acme.com")
    assert len(jobs) == 1


def test_parse_careers_page_empty_inputs():
    assert parse_careers_page("", "https://acme.com") == []
    assert parse_careers_page("<a href='/careers/x'>X</a>", "") == []


def test_careers_source_plan_prefers_careers_url():
    src = CareersPageSource()
    acct = Account(domain="acme.com", name="Acme", careers_url="https://jobs.acme.com/open")
    tasks = src.plan(acct, None)
    assert tasks[0].url == "https://jobs.acme.com/open"
    fallback_acct = Account(domain="acme.com", name="Acme")
    assert src.plan(fallback_acct, None)[0].url == "https://acme.com/careers"
    assert src.key == "ats_careers_page"
    assert src.requires == ()  # fires for accounts without ats_token (gate at vendor check)
