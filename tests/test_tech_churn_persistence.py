"""Regression: tech_churn must persist (was rejected as unknown).

The P1 tech-change-detection feature emitted `tech_churn` from
`diff_technologies`, but the type was absent from signals.yaml —
normalize_candidate raised unknown_signal_type and every churn candidate was
silently discarded while the technologies table still advanced, burning the
churn signal on every cycle. This test pins the taxonomy entry so the type
can never regress to unknown (same shape as test_review_trend_persistence).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.core.config import Config
from src.core.db import Database
from src.core.http import FetchResult
from src.core.models import Account, Document
from src.core.rawstore import RawStore
from src.core.runlog import RunContext
from src.identity.registry import AccountRegistry
from src.pipeline.runner import CollectorRunner
from src.signals.normalize import normalize_batch
from src.signals.store import SignalStore
from src.signals.taxonomy import Taxonomy
from src.sources.base import FetchTask, SourceAdapter
from src.sources.techstack.collector import TechstackSource
from src.sources.techstack.diff import diff_technologies
from src.sources.techstack.dns_probe import DnsEvidence
from src.sources.techstack.fingerprint import TechMatch


def test_tech_churn_persists_through_normalize():
    tax = Taxonomy.load()
    assert "tech_churn" in tax._types, (
        "tech_churn missing from signals.yaml — tech-churn signals are being "
        "rejected as unknown and never persist"
    )
    changes = diff_technologies(
        {"hubspot", "salesforce"},
        {"salesforce"},
        domain="acme.com",
        today="2026-09-04",
    )
    churn = [cand for stype, cand in changes if stype == "tech_churn"]
    assert churn, "expected a churn candidate for the removed vendor"
    assert churn[0].natural_key == "techchg:acme.com:hubspot:2026-09-04"

    valid, rejected = normalize_batch(
        churn,
        account=Account(domain="acme.com", name="Acme"),
        source="techstack",
        taxonomy=tax,
        now="2026-09-04T00:00:00Z",
    )
    assert len(valid) == 1
    assert not rejected


# ── Challenge-unsolved cycles must not fabricate churn/removal ──────────────
#
# When a Cloudflare challenge goes unsolved the result still reaches the
# collector (runner.py rebuilds it so cloudflare-only can be recorded) and
# harvest_tech collapses to a [cloudflare]-only match. Without a guard the
# runner's techstack block then (a) diffs {cloudflare} against the prior
# vendor set -> tech_churn for EVERY prior vendor, and (b) upsert_technologies
# advances missing_runs on every row -> false tech_removed two cycles later.
# Harness shape mirrors test_renewal_wiring.py (real clock; deltas sit far
# from window edges so a midnight rollover cannot flip an assertion).

CF_CHALLENGE = b"<html><head><title>Just a moment...</title></head><body></body></html>"


def _no_dns_probe(domain, *args, **kwargs):
    """Offline stand-in for probe_dns: an empty DnsEvidence makes the dns-delta
    path persist/emit nothing and the harvest's DNS merge add no vendors, so
    the harvest collapses to cloudflare-only exactly as in a real
    challenge-unsolved cycle."""
    return DnsEvidence()


class _ChallengeFetch:
    """Fetcher that always answers with a 403 CF challenge body — the wire
    shape _fetch_one's bypass waterfall sees before flagging
    _cloudflare_unsolved (the runner then rebuilds the result at 200 with the
    challenge doc, the exact shape runner.py:418-441 produces)."""

    def get(self, task, *, etag=None, last_modified=None):
        doc = Document(
            doc_id="d", source=task.source, url=task.url,
            domain=task.domain, body=CF_CHALLENGE, status=403,
        )
        return FetchResult(True, 403, doc, False, None, 1)


class _HealthyFetch:
    def __init__(self, by_url: dict):
        self.by_url = by_url

    def get(self, task, *, etag=None, last_modified=None):
        doc = Document(
            doc_id="d", source=task.source, url=task.url,
            domain=task.domain, body=bytes(self.by_url[task.url]), status=200,
        )
        return FetchResult(True, 200, doc, False, None, 1)


def _harness(tmp_path, *, fetcher, bypass=None, first_seen_days_ago: dict | None = None):
    """CollectorRunner over a db pre-seeded with technologies rows."""
    db = Database(tmp_path / "s.db")
    today = datetime.now(timezone.utc).date()
    for vendor, days_ago in (first_seen_days_ago or {}).items():
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
    runner = CollectorRunner(
        cfg, db, AccountRegistry(db), store, fetcher, SignalStore(db, tax), tax, ctx,
        cloudflare_bypass=bypass,
    )
    return runner, db, ctx


def test_challenge_unsolved_cycle_skips_diff_and_missing_runs(tmp_path, monkeypatch):
    """A cycle whose html task ended cloudflare_unsolved is no-evidence for
    change detection: it must not emit tech_churn/tech_removed for the prior
    vendors and must not advance their missing_runs — the next healthy cycle
    catches up honestly instead of fabricating churn for the whole stack."""
    monkeypatch.setattr("src.sources.techstack.dns_probe.probe_dns", _no_dns_probe)
    bypass = MagicMock()
    bypass.attempt.return_value = SimpleNamespace(success=False, result=None)
    runner, db, ctx = _harness(
        tmp_path,
        fetcher=_ChallengeFetch(),
        bypass=bypass,
        first_seen_days_ago={"hubspot": 200, "gtm": 100},
    )
    accounts = [Account(domain="acme.com", name="Acme")]
    runner.run([TechstackSource()], accounts, force=True)

    # No tech_churn: diffing the cloudflare-only harvest against the prior
    # vendor set must not brand every prior vendor as churned.
    churn = db.query("SELECT * FROM signals WHERE signal_type='tech_churn'")
    assert churn == []
    # No tech_removed either — removals require missing_runs to advance, which
    # an evidence-less cycle must never do.
    removed = db.query("SELECT * FROM signals WHERE signal_type='tech_removed'")
    assert removed == []
    # The prior rows survive untouched: still seen, missing_runs never moved.
    rows = db.query(
        "SELECT vendor, missing_runs FROM technologies "
        "WHERE domain='acme.com' AND vendor IN ('hubspot','gtm')"
    )
    assert {r["vendor"]: int(r["missing_runs"]) for r in rows} == {
        "hubspot": 0, "gtm": 0,
    }
    ctx.__exit__(None, None, None)


class _TechStub(SourceAdapter):
    """Techstack-shaped adapter (test_renewal_wiring.py pattern): harvest_tech
    returns fixed matches so the runner's diff/upsert block runs offline."""

    key = "techstack"
    tier = "http"
    cadence_hours = 168

    def plan(self, account, cursor):
        return [FetchTask(source=self.key, url=f"https://{account.domain}/", domain=account.domain)]

    def parse(self, doc, account, task_meta):
        return []

    def harvest_tech(self, doc, account, task_meta):
        return [
            TechMatch("salesforce", "Salesforce", ["crm"], "enterprise", "script_src", 0.8)
        ]


def test_healthy_cycle_still_emits_churn(tmp_path, monkeypatch):
    """Inverse sanity pin: on a HEALTHY cycle the diff pass still persists
    tech_churn for a vendor that really vanished — the challenge guard must
    suppress only evidence-less cycles, not change detection itself."""
    monkeypatch.setattr("src.sources.techstack.dns_probe.probe_dns", _no_dns_probe)
    runner, db, ctx = _harness(
        tmp_path,
        fetcher=_HealthyFetch({"https://acme.com/": b"<html><body>ok</body></html>"}),
        first_seen_days_ago={"hubspot": 200},
    )
    accounts = [Account(domain="acme.com", name="Acme")]
    runner.run([_TechStub()], accounts, force=True)

    churn = db.query("SELECT * FROM signals WHERE signal_type='tech_churn'")
    assert len(churn) == 1
    assert churn[0]["domain"] == "acme.com"
    assert churn[0]["source"] == "techstack"
    assert churn[0]["title"] == "hubspot"
    ctx.__exit__(None, None, None)


# ── Flap-guard persistence: extra_data["tech_flap"] ─────────────────────────
#
# diff_technologies accepts flap_guard (vendors that churned in the
# IMMEDIATELY PRIOR diff are suppressed from churn emission) but the runner
# never passed it and nothing persisted the prior-churn set, so a flapping
# vendor emitted churn on every cycle. Wiring contract: each cycle the runner
# REPLACES extra_data["tech_flap"] with the vendors that churned in THIS diff
# (a churn-free cycle stores [] and un-guards) via the task-meta registry,
# the same MERGE pattern _dns_delta uses for dns_evidence.

_HUBSPOT = TechMatch("hubspot", "HubSpot", ["crm"], "mid", "script_src", 0.8)
_SALESFORCE = TechMatch("salesforce", "Salesforce", ["crm"], "mid", "script_src", 0.8)


class _FlapStub(SourceAdapter):
    """_TechStub variant whose harvested vendor set is settable per cycle, so
    one db can be driven through absent -> present -> absent sequences."""

    key = "techstack"
    tier = "http"
    cadence_hours = 168

    def __init__(self):
        self.matches: list[TechMatch] = []

    def plan(self, account, cursor):
        return [FetchTask(source=self.key, url=f"https://{account.domain}/", domain=account.domain)]

    def parse(self, doc, account, task_meta):
        return []

    def harvest_tech(self, doc, account, task_meta):
        return list(self.matches)


def _stored_flap(db):
    """The persisted guard set, read the way production reads it: from the
    accounts table through the registry (extra_data JSON round-trip)."""
    acct = AccountRegistry(db).get("acme.com")
    if acct is None:
        return None
    return (acct.extra_data or {}).get("tech_flap")


def test_flap_guard_persists_prior_churn_set(tmp_path, monkeypatch):
    """Three cycles — hubspot absent (churn emitted, guard stored) -> present
    (guard CLEARED: the guard covers only the immediately prior diff, so a
    churn-free cycle overwrites the stored set with []) -> absent again (the
    churn is honest and re-arms the guard). Pins the REPLACE semantics:
    empty churn must still upsert."""
    monkeypatch.setattr("src.sources.techstack.dns_probe.probe_dns", _no_dns_probe)
    runner, db, ctx = _harness(
        tmp_path,
        fetcher=_HealthyFetch({"https://acme.com/": b"<html><body>ok</body></html>"}),
        first_seen_days_ago={"hubspot": 200},
    )
    stub = _FlapStub()
    accounts = [Account(domain="acme.com", name="Acme")]

    # Cycle 1: hubspot vanishes -> tech_churn persisted, guard armed.
    stub.matches = [_SALESFORCE]
    runner.run([stub], accounts, force=True)
    churn = db.query("SELECT * FROM signals WHERE signal_type='tech_churn'")
    assert [r["title"] for r in churn] == ["hubspot"]
    assert _stored_flap(db) == ["hubspot"]

    # Cycle 2: hubspot returns -> nothing to diff, guard cleared (REPLACE).
    accounts = [AccountRegistry(db).get("acme.com") or accounts[0]]
    stub.matches = [_HUBSPOT, _SALESFORCE]
    runner.run([stub], accounts, force=True)
    assert _stored_flap(db) == []

    # Cycle 3: hubspot vanishes again -> honest churn, guard re-armed.
    accounts = [AccountRegistry(db).get("acme.com") or accounts[0]]
    stub.matches = [_SALESFORCE]
    runner.run([stub], accounts, force=True)
    assert _stored_flap(db) == ["hubspot"]
    ctx.__exit__(None, None, None)


def test_flap_guard_suppresses_immediate_rechurn(tmp_path, monkeypatch):
    """hubspot absent for TWO consecutive cycles: cycle 1 emits tech_churn and
    arms the guard; cycle 2's re-churn (the row is still in technologies
    pending the gone threshold) must be SUPPRESSED — exactly one churn signal
    total — while the gone path still emits tech_removed. The clock is faked
    to advance one day per cycle so the would-be second churn carries its own
    natural key (same-day re-churns would merge into one row and hide the
    regression)."""
    monkeypatch.setattr("src.sources.techstack.dns_probe.probe_dns", _no_dns_probe)
    day1 = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    clock = {"now": day1}
    monkeypatch.setattr("src.pipeline.runner._now", lambda: clock["now"])
    runner, db, ctx = _harness(
        tmp_path,
        fetcher=_HealthyFetch({"https://acme.com/": b"<html><body>ok</body></html>"}),
        first_seen_days_ago={"hubspot": 200},
    )
    stub = _FlapStub()
    stub.matches = [_SALESFORCE]
    accounts = [Account(domain="acme.com", name="Acme")]

    # Cycle 1: hubspot vanishes -> churn emitted, guard armed.
    runner.run([stub], accounts, force=True)
    assert _stored_flap(db) == ["hubspot"]

    # Cycle 2 (next day): hubspot still absent -> re-churn suppressed.
    clock["now"] = day1 + timedelta(days=1)
    accounts = [AccountRegistry(db).get("acme.com") or accounts[0]]
    runner.run([stub], accounts, force=True)

    churn = db.query(
        "SELECT observed_at, title FROM signals WHERE signal_type='tech_churn' AND title='hubspot'"
    )
    assert [r["observed_at"] for r in churn] == [day1.date().isoformat()]
    assert _stored_flap(db) == []  # suppressed -> churned == {} clears the guard
    removed = db.query("SELECT title FROM signals WHERE signal_type='tech_removed'")
    assert [r["title"] for r in removed] == ["hubspot"]  # gone path unguarded
    ctx.__exit__(None, None, None)
