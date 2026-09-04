"""Wiring tests for renewal_window emission in the techstack diff pass.

Task 9: ``renewal_candidates()`` had zero production callers. These tests pin
the runner's techstack pass: stored ``technologies`` rows (vendor +
first_seen_at) are fed through the fingerprints.yaml contract-years map into
``renewal_candidates()``, persisted through the same ``_persist``/stats path
as the install/churn diff, and deduped by natural key on re-runs.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

from src.core.config import Config
from src.core.db import Database
from src.core.http import FetchResult
from src.core.models import Account, Document
from src.core.rawstore import RawStore
from src.core.runlog import RunContext
from src.identity.registry import AccountRegistry
from src.pipeline.runner import CollectorRunner, _renewal_cands
from src.signals.store import SignalStore
from src.signals.taxonomy import Taxonomy
from src.sources.base import FetchTask, SourceAdapter
from src.sources.techstack.fingerprint import TechMatch, load_fingerprint_rules

# ── Unit: the runner's wiring helper (INJECTED today — never the clock) ───

TODAY = date(2026, 9, 4)


def test_unit_emits_candidate_inside_lead_window():
    rules = load_fingerprint_rules()
    # first_seen 2025-11-09 -> default 1-year renewal 2026-11-09, 66 days
    # out: inside the 30..120 day lead window -> emitted.
    rows = [{"vendor": "hubspot", "first_seen_at": "2025-11-09"}]
    cands = _renewal_cands("acme.com", rows, TODAY, rules)
    assert len(cands) == 1
    cand = cands[0]
    assert cand.signal_type == "renewal_window"
    assert cand.natural_key == "renewal:hubspot:2026-11-09"
    assert cand.evidence_data["competitor"] == "hubspot"
    assert cand.evidence_data["first_seen"] == "2025-11-09"
    assert cand.evidence_data["renewal_estimate"] == "2026-11-09"


def test_unit_contract_years_map_from_fingerprints():
    rules = load_fingerprint_rules()
    # fingerprints.yaml pins workday to contract_years: 3: first_seen + 3
    # calendar years = 2026-11-09, inside the lead window -> emitted with a
    # 3-year natural key (not the 1-year default).
    rows = [{"vendor": "workday", "first_seen_at": "2023-11-09"}]
    cands = _renewal_cands("acme.com", rows, TODAY, rules)
    assert [c.natural_key for c in cands] == ["renewal:workday:2026-11-09"]


def test_unit_outside_window_or_unseen_vendor_is_silent():
    rules = load_fingerprint_rules()
    # workday first seen ~13 months ago -> its 3-year renewal is ~2 years
    # out, past the lead-window horizon -> no candidate. A row with no
    # first_seen_at is skipped entirely.
    rows = [
        {"vendor": "workday", "first_seen_at": (TODAY - timedelta(days=395)).isoformat()},
        {"vendor": "hubspot", "first_seen_at": None},
    ]
    assert _renewal_cands("acme.com", rows, TODAY, rules) == []


# ── Integration: the runner's techstack pass emits + dedupes ──────────────


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


class _TechStub(SourceAdapter):
    """Techstack-shaped adapter: harvest_tech returns fixed matches so the
    runner's technologies diff/renewal block runs without real HTTP."""

    key = "techstack"
    tier = "http"
    cadence_hours = 168

    def plan(self, account, cursor):
        return [FetchTask(source=self.key, url=f"https://{account.domain}/", domain=account.domain)]

    def parse(self, doc, account, task_meta):
        return []

    def harvest_tech(self, doc, account, task_meta):
        return [
            TechMatch("hubspot", "HubSpot", ["crm"], "mid", "script_src", 0.8),
            TechMatch("workday", "Workday", ["hris"], "enterprise", "script_src", 0.8),
        ]


def _harness(tmp_path, first_seen_days_ago: dict):
    """Runner over a db pre-seeded with technologies rows.

    Seeding computes first_seen relative to the real clock because the
    runner itself runs on the real clock (deltas sit far from the window
    edges, so a midnight rollover cannot flip an assertion).
    """
    db = Database(tmp_path / "s.db")
    today = date.today()
    for vendor, days_ago in first_seen_days_ago.items():
        seen = (today - timedelta(days=days_ago)).isoformat()
        db.upsert(
            "technologies",
            {
                "domain": "acme.com", "vendor": vendor, "category": "crm",
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


def test_runner_emits_and_dedupes_renewal_window(tmp_path):
    runner, db, ctx = _harness(tmp_path, {"hubspot": 300, "workday": 395})
    accounts = [Account(domain="acme.com", name="Acme")]
    adapters = [_TechStub()]
    stats1 = runner.run(adapters, accounts, force=True)
    # Exactly one renewal_window: hubspot (1y default contract, renewal ~65
    # days out); workday's 3y renewal is ~2 years out -> silent. The diff
    # emits nothing (prev set == current set), so the counts are all renewal.
    rows = db.query("SELECT * FROM signals WHERE signal_type='renewal_window'")
    assert len(rows) == 1
    row = rows[0]
    assert row["domain"] == "acme.com"
    assert row["source"] == "techstack"
    hubspot_seen = date.today() - timedelta(days=300)
    assert row["title"] == f"hubspot renewal ~{hubspot_seen.replace(year=hubspot_seen.year + 1).isoformat()}"
    ev = json.loads(row["evidence_data"])
    assert ev["competitor"] == "hubspot"
    assert ev["basis"] == "wayback_first_seen"
    assert stats1.signals_new == 1
    assert stats1.by_source["techstack"]["signals_new"] == 1
    assert stats1.candidates == 1
    # Re-run: identical natural keys -> dedupe, zero new signals.
    stats2 = runner.run(adapters, accounts, force=True)
    rows2 = db.query("SELECT * FROM signals WHERE signal_type='renewal_window'")
    assert len(rows2) == 1
    assert stats2.signals_new == 0
    ctx.__exit__(None, None, None)


# ── Review fix: Feb-29 first_seen_at must clamp, not crash ──────────────────


def test_renewal_candidates_feb29_first_seen_does_not_crash():
    """A Feb-29 first_seen_at must clamp to Feb 28 on non-leap renewal years.
    Pre-fix, date(2025, 2, 29) raised ValueError on EVERY cycle for that
    domain and the runner's fail-open catch silently emitted zero candidates
    forever (first_seen never changes)."""
    from src.sources.wayback.renewal import renewal_candidates

    rows = [{"vendor": "hubspot", "first_seen_at": "2024-02-29"}]
    # 1-year renewal from 2024-02-29 clamps to 2025-02-28; scanning forward,
    # the 2026-02-28 clamped anniversary lands inside the widened window.
    cands = renewal_candidates(
        "acme.com",
        rows,
        contract_years={},
        default_years=1,
        today=date(2025, 4, 1),
        lead_days=(0, 400),
    )
    assert [c.natural_key for c in cands] == ["renewal:hubspot:2026-02-28"]
