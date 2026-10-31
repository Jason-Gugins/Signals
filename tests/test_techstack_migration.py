"""Diff-inferred tech migrations — pure inference contract tests (Task A3).

A REMOVED vendor and an ADDED vendor sharing a fingerprint category in the
same cycle is a replacement: tech_migration_mentioned (w24, primary). Pure
module: category_of injected, today injected, no I/O.
"""
from __future__ import annotations

from src.sources.techstack.migration import infer_migrations

CATS = {
    "intercom": {"support", "chat"},
    "zendesk": {"support"},
    "segment": {"cdp"},
    "mktoweb": set(),
}


def category_of(vendor):
    return CATS.get(vendor, set())


def test_replacement_same_category():
    cands = infer_migrations({"zendesk"}, {"intercom"}, category_of, domain="acme.com", today="2026-10-30")
    assert len(cands) == 1
    c = cands[0]
    assert c.signal_type == "tech_migration_mentioned"
    assert c.natural_key == "techmig:acme.com:intercom:zendesk:2026-10"
    assert c.confidence == 0.65
    assert c.evidence_data["from"] == "intercom" and c.evidence_data["to"] == "zendesk"
    assert c.evidence_data["inferred"] is True
    assert c.evidence_data["categories"] == ["support"]


def test_no_shared_category_no_signal():
    assert infer_migrations({"segment"}, {"intercom"}, category_of, domain="a.com", today="2026-10-30") == []
    # removed vendor with no category can never migrate
    assert infer_migrations({"intercom"}, {"mktoweb"}, category_of, domain="a.com", today="2026-10-30") == []


def test_flap_guard_suppresses_both_sides():
    assert infer_migrations({"zendesk"}, {"intercom"}, category_of, domain="a.com",
                            today="2026-10-30", flap_guard={"intercom"}) == []
    assert infer_migrations({"zendesk"}, {"intercom"}, category_of, domain="a.com",
                            today="2026-10-30", flap_guard={"zendesk"}) == []


def test_monthly_key_dedupes():
    a = infer_migrations({"zendesk"}, {"intercom"}, category_of, domain="a.com", today="2026-10-01")[0]
    b = infer_migrations({"zendesk"}, {"intercom"}, category_of, domain="a.com", today="2026-10-29")[0]
    assert a.natural_key == b.natural_key


# ── Runner wiring: the diff block persists inferred migrations ──────────────
#
# Mirror of tests/test_tech_churn_persistence.py's fake-runner harness: a real
# CollectorRunner over a temp db, a stub techstack adapter whose harvest_tech
# is settable, DNS probing stubbed offline. The store flips zendesk -> intercom
# in one cycle (both share the "support" fingerprint category in
# config/fingerprints.yaml); the wiring must land exactly one
# tech_migration_mentioned row in the signal store, persisted through the same
# _persist path the diff's tech_install_new/tech_churn candidates use.

import json  # noqa: E402  (runner-wiring half; pure tests above stay import-light)
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

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
from src.sources.techstack.dns_probe import DnsEvidence
from src.sources.techstack.fingerprint import TechMatch


def _no_dns_probe(domain, *args, **kwargs):
    return DnsEvidence()


class _HealthyFetch:
    def __init__(self, by_url: dict):
        self.by_url = by_url

    def get(self, task, *, etag=None, last_modified=None):
        doc = Document(
            doc_id="d", source=task.source, url=task.url,
            domain=task.domain, body=bytes(self.by_url[task.url]), status=200,
        )
        return FetchResult(True, 200, doc, False, None, 1)


class _FlipStub(SourceAdapter):
    """Techstack-shaped adapter (test_renewal_wiring.py pattern) whose
    harvested matches are settable, so one db can be driven through a
    vendor swap."""

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


def _harness(tmp_path, *, fetcher, first_seen_days_ago: dict | None = None):
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
        cloudflare_bypass=MagicMock(),
    )
    return runner, db, ctx


def test_runner_persists_inferred_migration_same_cycle(tmp_path, monkeypatch):
    """One cycle where the harvested stack swaps zendesk (seeded, support
    category) for intercom (support, chat) must persist exactly one
    tech_migration_mentioned — inferred from the same diff the churn pass
    already computes, flap-guarded by the same set."""
    monkeypatch.setattr("src.sources.techstack.dns_probe.probe_dns", _no_dns_probe)
    runner, db, ctx = _harness(
        tmp_path,
        fetcher=_HealthyFetch({"https://acme.com/": b"<html><body>ok</body></html>"}),
        first_seen_days_ago={"zendesk": 200},
    )
    stub = _FlipStub()
    stub.matches = [TechMatch("intercom", "Intercom", ["support"], "mid", "script_src", 0.8)]
    accounts = [Account(domain="acme.com", name="Acme")]
    runner.run([stub], accounts, force=True)

    rows = db.query("SELECT * FROM signals WHERE signal_type='tech_migration_mentioned'")
    assert len(rows) == 1, f"expected exactly one migration signal, got {rows}"
    row = rows[0]
    assert row["domain"] == "acme.com"
    assert row["source"] == "techstack"
    assert row["title"] == "zendesk -> intercom"
    ev = json.loads(row["evidence_data"])
    assert ev["from"] == "zendesk" and ev["to"] == "intercom"  # from=removed, to=added
    assert ev["inferred"] is True
    assert "support" in ev["categories"]
    ctx.__exit__(None, None, None)
