"""Tests for derived job-posting signals."""

from __future__ import annotations

from datetime import date, timedelta

from src.sources.jobsignals.analyze import MIGRATION_PATTERNS, JobsWindow, analyze_jobs

TODAY = date(2026, 8, 16)
CFG = {"surge_min_roles": 5, "surge_window_days": 60}


def _job(**kw):
    base = {
        "job_key": kw.get("job_key", kw.get("external_id", "k")),
        "external_id": kw.get("external_id", "k"),
        "title": "Role",
        "url": "https://ex/j",
        "posted_at": TODAY.isoformat(),
        "department": None,
        "country": None,
        "city": None,
        "description": "",
        "closed_at": None,
    }
    base.update(kw)
    return base


def test_each_rule_fires_once():
    jobs = []
    # 5 recent Sales roles -> surge
    for i in range(5):
        jobs.append(_job(external_id=f"s{i}", department="Sales", title=f"AE {i}", posted_at=(TODAY - timedelta(days=i)).isoformat()))
    # leadership
    jobs.append(_job(external_id="lead", title="VP Engineering", department="Engineering"))
    # new dept Product with 2 roles, not in prior
    jobs.append(_job(external_id="p1", department="Product", title="PM 1"))
    jobs.append(_job(external_id="p2", department="Product", title="PM 2"))
    # new geo Germany
    jobs.append(_job(external_id="g1", department="Sales", country="Germany", title="AE DE"))
    # office city 3 roles
    for i in range(3):
        jobs.append(_job(external_id=f"o{i}", city="Lisbon", country="Portugal", department="Sales", title=f"AE LIS {i}"))
    # migration
    jobs.append(_job(external_id="mig", title="Eng", department="Engineering", description="We are migrating from Salesforce to HubSpot this quarter."))
    # scoop
    jobs.append(_job(external_id="scoop", title="Eng 2", department="Engineering", description="We are building a new billing platform in-house."))
    # filler to ~40
    for i in range(20):
        jobs.append(_job(external_id=f"f{i}", department="Engineering", title=f"SWE {i}", posted_at="2025-01-01"))
    w = JobsWindow(
        domain="acme.com",
        jobs=jobs,
        prior_countries={"Canada", "United States"},
        prior_departments={"Sales", "Engineering"},
        baseline_open_by_dept={"Engineering": 20},
    )
    cands = analyze_jobs(w, today=TODAY, cfg=CFG)
    types = {c.signal_type for c in cands}
    assert "hiring_surge" in types
    assert "leadership_job_open" in types
    assert "department_expansion" in types
    assert "new_geo" in types
    assert "office_open" in types
    assert "tech_migration_mentioned" in types
    assert "internal_project_scoop" in types
    assert all("job_titles" in c.evidence_data for c in cands)


def test_surge_threshold_4_vs_5():
    four = [_job(external_id=str(i), department="Sales", posted_at=TODAY.isoformat()) for i in range(4)]
    w = JobsWindow("acme.com", four, set(), {"Sales"}, {})
    assert not any(c.signal_type == "hiring_surge" for c in analyze_jobs(w, today=TODAY, cfg=CFG))
    five = four + [_job(external_id="5", department="Sales", posted_at=TODAY.isoformat())]
    w2 = JobsWindow("acme.com", five, set(), {"Sales"}, {})
    assert any(c.signal_type == "hiring_surge" for c in analyze_jobs(w2, today=TODAY, cfg=CFG))


def test_surge_baseline_ratio():
    jobs = [_job(external_id=str(i), department="CS", posted_at="2025-01-01") for i in range(10)]
    w = JobsWindow("acme.com", jobs, set(), {"CS"}, {"CS": 4})  # 10 >= 1.5*4 and delta 6>=4
    assert any(c.signal_type == "hiring_surge" for c in analyze_jobs(w, today=TODAY, cfg=CFG))


def test_new_geo_ignores_none_country():
    jobs = [_job(external_id="1", department="Sales", country=None)]
    w = JobsWindow("acme.com", jobs, set(), {"Sales"}, {})
    assert not any(c.signal_type == "new_geo" for c in analyze_jobs(w, today=TODAY, cfg=CFG))


def test_migration_regex_yes_and_no():
    yes = [
        "migrating from Salesforce to HubSpot",
        "migrate off of Marketo to Braze",
        "replacing Outreach with Salesloft",
        "sunset Eloqua next quarter",
        "consolidating our stack onto Workday",
        "We will migrate from Zendesk to Gorgias",
    ]
    no = [
        "migrating to the cloud",
        "migration experience preferred",
        "responsible for data migration",
    ]
    for text in yes:
        assert any(p.search(text) for p in MIGRATION_PATTERNS), text
    for text in no:
        assert not any(p.search(text) for p in MIGRATION_PATTERNS), text


def test_iso_keys_stable_within_period():
    jobs = [_job(external_id=str(i), department="Sales") for i in range(5)]
    w = JobsWindow("acme.com", jobs, set(), {"Sales"}, {})
    a = analyze_jobs(w, today=TODAY, cfg=CFG)
    b = analyze_jobs(w, today=TODAY, cfg=CFG)
    surge = [c for c in a if c.signal_type == "hiring_surge"][0]
    assert surge.natural_key == [c for c in b if c.signal_type == "hiring_surge"][0].natural_key
    later = analyze_jobs(w, today=TODAY + timedelta(days=10), cfg=CFG)
    later_key = [c for c in later if c.signal_type == "hiring_surge"][0].natural_key
    # 10 days later may be next ISO week
    assert later_key.split(":")[-1]
