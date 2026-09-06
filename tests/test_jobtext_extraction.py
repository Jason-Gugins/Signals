"""Task 12 (roadmap 2026-09-04): ATS job-description extraction deepening.

The jobsignals job-text extraction used to under-fire: vendor captures inside
migration phrasing required capitalized names, only the FIRST match per job
survived, and required-stack phrasing ("experience with X") was not read at
all. These tests pin the deepened, purely additive extraction:

- case-insensitive vendor matching against the known-name allowlist
  (derived from the techstack fingerprint vendor keys),
- required-stack phrasing captured into evidence_data["required_stack"],
- all matches per job aggregated into one candidate per (job, signal_type)
  (first migration pair in from_tech/to_tech, the rest in also_mentioned),
- precision guards: word boundaries (no "salesforceable") and B2B-context
  requirements for common-word vendor names ("monday").

No new signal types: everything rides the existing
``tech_migration_mentioned`` / ``internal_project_scoop`` candidates.
"""

from __future__ import annotations

from datetime import date

from src.core.models import Account
from src.signals.normalize import normalize_batch
from src.signals.taxonomy import Taxonomy
from src.sources.jobsignals.analyze import JobsWindow, analyze_jobs

TODAY = date(2026, 8, 16)
CFG = {"surge_min_roles": 5, "surge_window_days": 60}


def _job(desc, external_id="k1", **kw):
    base = {
        "job_key": external_id,
        "external_id": external_id,
        "title": "Role",
        "url": f"https://ex/{external_id}",
        "posted_at": TODAY.isoformat(),
        "department": None,
        "country": None,
        "city": None,
        "description": desc,
        "closed_at": None,
    }
    base.update(kw)
    return base


def _window(jobs):
    return JobsWindow(
        domain="acme.com",
        jobs=jobs,
        prior_countries=set(),
        prior_departments=set(),
        baseline_open_by_dept={},
    )


def _migrations(cands):
    return [c for c in cands if c.signal_type == "tech_migration_mentioned"]


def _ev_strings(cand) -> set[str]:
    """Every string value in the evidence tree, casefolded."""
    out: set[str] = set()

    def walk(v):
        if isinstance(v, str):
            out.add(v.casefold())
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)

    walk(cand.evidence_data)
    return out


# ── case-insensitive vendor matching (the old code missed lowercase) ────────


def test_bare_stack_mention_does_not_assert_migration():
    # Review fix: tech_migration_mentioned asserts a MIGRATION. A bare stack
    # statement fires nothing — the evidence template would render
    # "migrating from  to X" with an empty from_tech.
    cands = analyze_jobs(
        _window([_job("Our stack is built on snowflake and salesforce.", external_id="low")]),
        today=TODAY,
        cfg=CFG,
    )
    assert not _migrations(cands)

    # The lowercase-recall win lives in MIGRATION phrasing (see the allowlist
    # test below): mentions still enrich real migration candidates' evidence.
    cands = analyze_jobs(
        _window(
            [
                _job(
                    "We are migrating off of marketo to braze. "
                    "Our stack is also built on snowflake.",
                    external_id="low2",
                )
            ]
        ),
        today=TODAY,
        cfg=CFG,
    )
    migs = _migrations(cands)
    assert migs and migs[0].evidence_data["from_tech"] == "marketo"
    blob = _ev_strings(migs[0])
    assert "snowflake" in blob


def test_lowercase_migration_pair_resolved_via_allowlist():
    cands = analyze_jobs(
        _window([_job("we are migrating off of marketo to braze next quarter", external_id="relax")]),
        today=TODAY,
        cfg=CFG,
    )
    migs = _migrations(cands)
    assert migs, "lowercase migration phrasing with known vendors must fire"
    assert migs[0].evidence_data["from_tech"] == "marketo"
    assert migs[0].evidence_data["to_tech"] == "braze"


# ── required-stack phrasing ──────────────────────────────────────────────────


def test_required_stack_experience_with():
    cands = analyze_jobs(
        _window(
            [
                _job(
                    "We are migrating off of marketo to braze. "
                    "You will need experience with Workday. "
                    "Bonus: proficiency in Snowflake.",
                    external_id="req",
                )
            ]
        ),
        today=TODAY,
        cfg=CFG,
    )
    migs = _migrations(cands)
    assert migs, "required-stack phrasing rides a real migration candidate's evidence"
    req = migs[0].evidence_data.get("required_stack") or []
    assert "workday" in req
    assert "snowflake" in req


def test_required_stack_without_known_vendor_stays_silent():
    cands = analyze_jobs(
        _window([_job("experience with data migration and stakeholder management", external_id="noreq")]),
        today=TODAY,
        cfg=CFG,
    )
    assert not _migrations(cands)


# ── precision guards ────────────────────────────────────────────────────────


def test_word_boundary_blocks_embedded_vendor():
    cands = analyze_jobs(
        _window([_job("We need someone salesforceable and driven to join us.", external_id="bound")]),
        today=TODAY,
        cfg=CFG,
    )
    assert not _migrations(cands)


def test_ambiguous_common_word_requires_b2b_context():
    # "monday" alone (day of week) must not match...
    alone = analyze_jobs(
        _window([_job("Monday standups and sprint planning fill the week.", external_id="mon1")]),
        today=TODAY,
        cfg=CFG,
    )
    assert not _migrations(alone)
    # ...and even with B2B context, a bare mention must not fabricate a
    # migration — it only enriches a real migration candidate's evidence.
    ctx = analyze_jobs(
        _window(
            [
                _job(
                    "We are migrating off of marketo to braze. "
                    "Own our monday crm integration and admin work.",
                    external_id="mon2",
                )
            ]
        ),
        today=TODAY,
        cfg=CFG,
    )
    migs = _migrations(ctx)
    assert migs, "the migration pair still fires"
    assert migs[0].evidence_data["from_tech"] == "marketo"
    assert "monday" in _ev_strings(migs[0])


# ── all matches per job, aggregated into one candidate ──────────────────────


def test_multiple_migration_pairs_first_plus_also_mentioned():
    cands = analyze_jobs(
        _window(
            [
                _job(
                    "We are migrating from Salesforce to HubSpot this quarter. "
                    "Next, we are replacing Zendesk with Intercom.",
                    external_id="multi",
                )
            ]
        ),
        today=TODAY,
        cfg=CFG,
    )
    migs = _migrations(cands)
    assert len(migs) == 1, "one candidate per (job, signal_type)"
    ev = migs[0].evidence_data
    assert ev["from_tech"] == "Salesforce"
    assert ev["to_tech"] == "HubSpot"
    also = ev.get("also_mentioned") or []
    assert {"from": "Zendesk", "to": "Intercom"} in also


def test_capitalized_first_pair_behavior_unchanged():
    cands = analyze_jobs(
        _window([_job("We are migrating from Salesforce to HubSpot this quarter.", external_id="old")]),
        today=TODAY,
        cfg=CFG,
    )
    migs = _migrations(cands)
    assert len(migs) == 1
    assert migs[0].evidence_data["from_tech"] == "Salesforce"
    assert migs[0].evidence_data["to_tech"] == "HubSpot"
    assert migs[0].natural_key == "mig:old:Salesforce:HubSpot"


# ── persistence (house rule: survives normalize_batch) ──────────────────────


def test_jobtext_candidate_persists_through_normalize_batch():
    cands = analyze_jobs(
        _window(
            [
                _job(
                    "We are migrating off of marketo to braze next quarter.",
                    external_id="persist",
                )
            ]
        ),
        today=TODAY,
        cfg=CFG,
    )
    cand = _migrations(cands)[0]
    valid, rejected = normalize_batch(
        [cand],
        account=Account(domain="acme.com", name="Acme"),
        source="jobsignals",
        taxonomy=Taxonomy.load(),
        now="2026-08-16T00:00:00Z",
    )
    assert len(valid) == 1
    assert rejected == []
    assert valid[0].signal_type == "tech_migration_mentioned"
