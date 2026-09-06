"""Wiring tests for the unreachable signal types (plan Task 4).

competitor_detected / tech_removed / backfill_open are registered in the
taxonomy but had no live production call site. These tests pin each wiring:

- 4a: techstack parse threads config/fingerprints.yaml ``competitors`` into
  ``tech_to_candidates`` so a named-vendor match emits competitor_detected.
- 4b: the runner captures ``upsert_technologies``'s (new, gone) return and
  persists tech_removed for confirmed removals (missing_runs >= 2).
- 4c: jobsignals local_harvest emits backfill_open when a recently-closed
  title is open again (casefold match, 120d window, monthly natural key).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone, timedelta

from src.core.config import Config
from src.core.db import Database
from src.core.http import FetchResult
from src.core.models import Account, Document
from src.core.rawstore import RawStore
from src.core.runlog import RunContext
from src.identity.registry import AccountRegistry
from src.pipeline.runner import CollectorRunner
from src.signals.store import SignalStore
from src.signals.taxonomy import Taxonomy
from src.sources.base import FetchTask, SourceAdapter
from src.sources.techstack.collector import TechstackSource, tech_to_candidates
from src.sources.techstack.fingerprint import TechMatch

TODAY = date(2026, 9, 4)


# ── 4a: competitor_detected ─────────────────────────────────────────────────


def test_tech_to_candidates_emits_competitor_detected():
    cands = tech_to_candidates(
        "acme.com", ["hubspot"], [], [], {"vendors": {}}, ["hubspot"], today=TODAY
    )
    hits = [c for c in cands if c.signal_type == "competitor_detected"]
    assert len(hits) == 1
    cand = hits[0]
    assert cand.natural_key == "competitor_detected:hubspot:2026-09"
    assert cand.title == "hubspot"
    assert cand.evidence_data == {"competitor": "hubspot"}


def test_tech_to_candidates_empty_competitors_emits_none():
    cands = tech_to_candidates(
        "acme.com", ["hubspot"], [], [], {"vendors": {}}, [], today=TODAY
    )
    assert not any(c.signal_type == "competitor_detected" for c in cands)


def test_parse_passes_fingerprint_competitors(monkeypatch):
    """parse must thread rules['competitors'] into tech_to_candidates.

    load_fingerprint_rules is imported function-locally inside parse, so the
    monkeypatch targets the fingerprint module attribute.
    """
    import src.sources.techstack.fingerprint as fingerprint

    rules = {
        "version": 1,
        "vendors": {
            "hubspot": {
                "display": "HubSpot",
                "category": ["crm"],
                "tier": "mid",
                "match": {"script_src": ["js.hs-scripts.com"]},
            }
        },
        "competitors": ["hubspot"],
    }
    monkeypatch.setattr(fingerprint, "load_fingerprint_rules", lambda: rules)
    doc = Document(
        doc_id="d1",
        source="techstack",
        url="https://acme.com/",
        domain="acme.com",
        body=(
            b"<html><head><script src='https://js.hs-scripts.com/123.js' defer>"
            b"</script></head><body><p>hello</p></body></html>"
        ),
        status=200,
    )
    cands = TechstackSource().parse(doc, Account(domain="acme.com", name="Acme"), {"today": "2026-09-04"})
    hits = [c for c in cands if c.signal_type == "competitor_detected"]
    assert len(hits) == 1
    assert hits[0].natural_key == "competitor_detected:hubspot:2026-09"


# ── 4b: tech_removed (runner wiring) ────────────────────────────────────────


class _FakeFetch:
    def __init__(self, by_url: dict):
        self.by_url = by_url

    def get(self, task, *, etag=None, last_modified=None):
        body = self.by_url[task.url]
        doc = Document(
            doc_id="d", source=task.source, url=task.url,
            domain=task.domain, body=bytes(body), status=200,
        )
        return FetchResult(True, 200, doc, False, None, 1)


class _TechRemovedStub(SourceAdapter):
    """Techstack-shaped adapter that no longer returns the seeded vendor."""

    key = "techstack"
    tier = "http"
    cadence_hours = 168

    def plan(self, account, cursor):
        return [FetchTask(source=self.key, url=f"https://{account.domain}/", domain=account.domain)]

    def parse(self, doc, account, task_meta):
        return []

    def harvest_tech(self, doc, account, task_meta):
        return [TechMatch("hubspot", "HubSpot", ["crm"], "mid", "script_src", 0.8)]


def _removed_harness(tmp_path):
    """Runner over a db pre-seeded with a 'zendesk' technologies row that the
    stub no longer returns. Seeding computes first_seen relative to the real
    clock (the runner runs on the real clock); 200 days back keeps zendesk's
    default 1-year renewal OUTSIDE the 30..120-day lead window so only
    tech_removed is under test."""
    db = Database(tmp_path / "s.db")
    today = (datetime.now(timezone.utc).date())
    seen = (today - timedelta(days=200)).isoformat()
    db.upsert(
        "technologies",
        {
            "domain": "acme.com", "vendor": "zendesk", "category": "support",
            "tier": "mid", "first_seen_at": seen, "last_seen_at": seen,
            "missing_runs": 0, "evidence": "script_src", "confidence": 0.8,
            "source": "techstack",
        },
        pk=("domain", "vendor"),
    )
    cfg = Config()
    cfg.http.max_workers = 2
    cfg.http.respect_robots = False
    store = RawStore(db, tmp_path / "raw")
    tax = Taxonomy.load()
    ctx = RunContext(db, "collect")
    ctx.__enter__()
    fetcher = _FakeFetch({"https://acme.com/": b"<html><body>ok</body></html>"})
    runner = CollectorRunner(
        cfg, db, AccountRegistry(db), store, fetcher, SignalStore(db, tax), tax, ctx
    )
    return runner, db, ctx


def test_runner_persists_tech_removed_after_two_missing_runs(tmp_path):
    runner, db, ctx = _removed_harness(tmp_path)
    accounts = [Account(domain="acme.com", name="Acme")]
    adapters = [_TechRemovedStub()]
    # Run 1: zendesk missing_runs 0->1 (unconfirmed) -> NO tech_removed.
    stats1 = runner.run(adapters, accounts, force=True)
    rows1 = db.query("SELECT * FROM signals WHERE signal_type='tech_removed'")
    assert rows1 == []
    assert stats1.signals_new == 2  # install(hubspot) + churn(zendesk) only
    # Run 2: zendesk missing_runs 1->2 (confirmed) -> tech_removed persisted.
    stats2 = runner.run(adapters, accounts, force=True)
    rows2 = db.query("SELECT * FROM signals WHERE signal_type='tech_removed'")
    assert len(rows2) == 1
    row = rows2[0]
    assert row["domain"] == "acme.com"
    assert row["source"] == "techstack"
    assert row["title"] == "zendesk"
    # The signals table stores the hashed natural key; assert through
    # make_signal_id that the monthly key shape is exactly the collector's.
    from src.signals.normalize import make_signal_id

    assert row["signal_id"] == make_signal_id(
        "acme.com", "tech_removed", f"tech_removed:zendesk:{(datetime.now(timezone.utc).date()):%Y-%m}"
    )
    assert row["observed_at"] == (datetime.now(timezone.utc).date()).isoformat()
    assert json.loads(row["evidence_data"]) == {"vendor": "zendesk"}
    assert stats2.signals_new == 1
    assert stats2.by_source["techstack"]["signals_new"] == 1
    # Same-month re-run: zendesk gone again but the natural key dedupes.
    stats3 = runner.run(adapters, accounts, force=True)
    rows3 = db.query("SELECT * FROM signals WHERE signal_type='tech_removed'")
    assert len(rows3) == 1
    assert stats3.signals_new == 0
    ctx.__exit__(None, None, None)


# ── 4c: backfill_open (jobsignals local derivation) ─────────────────────────

from src.signals.normalize import make_signal_id, normalize_batch  # noqa: E402
from src.sources.ats.common import JobPost, upsert_jobs  # noqa: E402
from src.sources.jobsignals.collector import JobSignalsSource  # noqa: E402

BACKFILL_TODAY = date(2026, 9, 5)


def _seed_backfill_jobs(db):
    """Six jobs: a recently-closed title that is open again under a new
    external_id (case-varied), a closed title NOT re-opened, an open-only
    title, and a re-opened pair whose closure is beyond the 120d window."""
    posted = (BACKFILL_TODAY - timedelta(days=30)).isoformat()
    jobs = [
        JobPost(external_id="1", title="Account Executive", url="https://x/1", posted_at=posted, department="Sales", country="Canada"),
        JobPost(external_id="42", title="account executive", url="https://x/42", posted_at=posted, department="Sales", country="Canada"),
        JobPost(external_id="2", title="Data Analyst", url="https://x/2", posted_at=posted, department="Data", country="Canada"),
        JobPost(external_id="3", title="Sales Manager", url="https://x/3", posted_at=posted, department="Sales", country="Canada"),
        JobPost(external_id="4", title="Recruiter", url="https://x/4", posted_at=posted, department="HR", country="Canada"),
        JobPost(external_id="5", title="Recruiter", url="https://x/5", posted_at=posted, department="HR", country="Canada"),
    ]
    upsert_jobs(db, "acme.com", jobs, "ats_greenhouse", now="2026-08-06T00:00:00", token="acme")
    for ext_id, closed_at in [
        ("1", (BACKFILL_TODAY - timedelta(days=10)).isoformat()),  # re-opened
        ("2", (BACKFILL_TODAY - timedelta(days=5)).isoformat()),  # closed, not re-opened
        ("4", (BACKFILL_TODAY - timedelta(days=200)).isoformat()),  # re-opened, too old
    ]:
        db.execute(
            "UPDATE jobs SET closed_at=? WHERE domain='acme.com' AND external_id=?",
            (closed_at, ext_id),
        )


def test_backfill_open_emitted_for_reopened_title(tmp_path):
    db = Database(tmp_path / "s.db")
    _seed_backfill_jobs(db)
    cands = JobSignalsSource().local_harvest(
        db=db, account=Account(domain="acme.com"), today=BACKFILL_TODAY, task_meta={}
    )
    bf = [c for c in cands if c.signal_type == "backfill_open"]
    assert len(bf) == 1
    cand = bf[0]
    assert cand.natural_key == "backfill:acme.com:account-executive:2026-09"
    assert cand.title == "account executive"  # the currently-open title
    assert cand.observed_at == "2026-09-05"
    assert cand.confidence == 0.6
    assert cand.evidence_data == {"title": "account executive", "closed_at": "2026-08-26"}


def test_backfill_open_persists_through_normalize_batch(tmp_path):
    db = Database(tmp_path / "s.db")
    _seed_backfill_jobs(db)
    cands = JobSignalsSource().local_harvest(
        db=db, account=Account(domain="acme.com"), today=BACKFILL_TODAY, task_meta={}
    )
    valid, rej = normalize_batch(
        cands,
        account=Account(domain="acme.com"),
        source="jobsignals",
        taxonomy=Taxonomy.load(),
        now="2026-09-05T00:00:00",
    )
    sigs = [s for s in valid if s.signal_type == "backfill_open"]
    assert len(sigs) == 1
    assert sigs[0].source == "jobsignals"
    assert sigs[0].domain == "acme.com"
    assert sigs[0].signal_id == make_signal_id(
        "acme.com", "backfill_open", "backfill:acme.com:account-executive:2026-09"
    )
    assert not any("backfill" in reason for _, reason in rej)
