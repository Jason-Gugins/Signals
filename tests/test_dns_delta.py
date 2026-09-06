"""Task 14 Feature 2 — SPF-include churn detection from DNS evidence snapshots.

The DNS probe's DnsEvidence (mx / txt / spf_includes / cname / ns) was
discarded after vendor matching, so a mail-vendor switch that removes an SPF
include no fingerprint rule names was invisible. This wave:

- persists a SNAPSHOT of the probe (mx, spf_includes, cname + observation
  date) into ``account.extra_data["dns_evidence"]`` through the SAME
  ``AccountRegistry`` the runner injects into parse ``task_meta``
  (``meta.setdefault("registry", ...)`` — the BBB pattern); merged, never
  clobbering other keys;
- when a prior snapshot exists, computes REMOVED spf_includes (in prior,
  absent now) and emits one ``tech_churn`` candidate per include that no
  fingerprint rule's ``spf_include`` needles claim (named vendors are
  already handled by the vendor-level diff);
- keeps the probe SINGLE per collect: a module-level cache keyed by
  domain+today is shared between parse() and harvest_tech().

Design choice (per plan Task 14): the snapshot/delta lives in ``parse()``
(option a) — candidates are parse's return type, the registry access and the
dns probe import are purity-safe, and ``today`` comes from task_meta (no
clock reads). A totally empty probe result (dnspython missing / total DNS
failure) never persists or diffs — probe failure must never masquerade as
churn.
"""

from __future__ import annotations

import pytest

from src.core.db import Database
from src.core.models import Account, Document
from src.identity.registry import AccountRegistry
from src.signals.normalize import make_signal_id, normalize_batch
from src.signals.taxonomy import Taxonomy
from src.sources.techstack.collector import TechstackSource
from src.sources.techstack.dns_probe import DnsEvidence

DOMAIN = "acme.com"
TODAY = "2026-09-05"


@pytest.fixture(autouse=True)
def _clear_dns_cache():
    """The per-(domain, day) probe cache must not leak between tests."""
    from src.sources.techstack import collector

    collector._DNS_CACHE.clear()
    yield
    collector._DNS_CACHE.clear()


def _registry(tmp_path) -> AccountRegistry:
    return AccountRegistry(Database(tmp_path / "reg.db"))


def _account(registry: AccountRegistry, extra: dict | None = None) -> Account:
    return registry.upsert(Account(domain=DOMAIN, name="Acme", extra_data=dict(extra or {})))


def _doc() -> Document:
    return Document(
        doc_id="d", source="techstack", url=f"https://{DOMAIN}/",
        body=b"<html><body>hi</body></html>",
    )


def _meta(registry) -> dict:
    return {"kind": "html", "today": TODAY, "registry": registry}


def _probe_returns(monkeypatch, ev: DnsEvidence, calls: list | None = None):
    from src.sources.techstack.dns_probe import probe_dns as _real

    def _fake(domain, **kw):
        if calls is not None:
            calls.append(domain)
        return ev

    monkeypatch.setattr("src.sources.techstack.dns_probe.probe_dns", _fake)
    return _real


# ── snapshot persistence (the BBB registry pattern) ──────────────────────────


def test_snapshot_persisted_to_extra_data_merged(tmp_path, monkeypatch):
    registry = _registry(tmp_path)
    _account(registry, extra={"seed": "keep"})
    _probe_returns(
        monkeypatch,
        DnsEvidence(
            mx=["aspmx.l.google.com"],
            spf_includes=["_spf.google.com"],
            cname={"status.acme.com": "acme.statuspage.io"},
        ),
    )

    cands = TechstackSource().parse(_doc(), registry.get(DOMAIN), _meta(registry))

    assert cands == []  # first observation: baseline only, no delta
    stored = registry.get(DOMAIN)
    snap = stored.extra_data["dns_evidence"]
    assert snap == {
        "observed_at": TODAY,
        "mx": ["aspmx.l.google.com"],
        "spf_includes": ["_spf.google.com"],
        "cname": {"status.acme.com": "acme.statuspage.io"},
    }
    # Merged, never clobbered (the bbb_url-style keys must survive).
    assert stored.extra_data["seed"] == "keep"


def test_parse_without_registry_never_probes_or_emits(tmp_path, monkeypatch):
    registry = _registry(tmp_path)
    _account(registry)

    def _must_not_probe(domain, **kw):
        raise AssertionError("DNS probe must not run without a registry")

    monkeypatch.setattr("src.sources.techstack.dns_probe.probe_dns", _must_not_probe)
    cands = TechstackSource().parse(_doc(), registry.get(DOMAIN), {"kind": "html", "today": TODAY})
    assert cands == []


# ── removal detection ────────────────────────────────────────────────────────


def test_removed_include_emits_tech_churn(tmp_path, monkeypatch):
    registry = _registry(tmp_path)
    _account(
        registry,
        extra={
            "dns_evidence": {
                "observed_at": "2026-08-01",
                "mx": ["aspmx.l.google.com"],
                "spf_includes": ["_spf.google.com", "mail.zendesk.com"],
                "cname": {},
            }
        },
    )
    _probe_returns(monkeypatch, DnsEvidence(spf_includes=["_spf.google.com"]))

    cands = TechstackSource().parse(_doc(), registry.get(DOMAIN), _meta(registry))

    assert len(cands) == 1
    c = cands[0]
    assert c.signal_type == "tech_churn"
    assert c.observed_at == TODAY
    assert c.natural_key == f"dnschurn:{DOMAIN}:mail.zendesk.com:2026-09"
    assert c.title == "SPF include removed: mail.zendesk.com"
    assert c.confidence == 0.5
    assert c.evidence_data == {
        "spf_include_removed": "mail.zendesk.com",
        "kind": "mail_vendor_switch",
    }
    # The snapshot advanced: the next cycle diffs against the CURRENT set.
    assert registry.get(DOMAIN).extra_data["dns_evidence"]["spf_includes"] == ["_spf.google.com"]


def test_named_vendor_include_removal_skipped(tmp_path, monkeypatch):
    registry = _registry(tmp_path)
    _account(
        registry,
        extra={
            "dns_evidence": {
                "observed_at": "2026-08-01",
                "mx": [],
                "spf_includes": ["hubspotemail.net", "mktomail.com"],
                "cname": {},
            }
        },
    )
    # mail.zendesk.com style: a probe that still sees the domain but no SPF
    # includes at all — hubspotemail.net/mktomail.com are NAMED vendors'
    # needles (hubspot / marketo), so the vendor-level diff handles them.
    _probe_returns(monkeypatch, DnsEvidence(mx=["aspmx.l.google.com"]))

    cands = TechstackSource().parse(_doc(), registry.get(DOMAIN), _meta(registry))

    assert cands == []
    assert registry.get(DOMAIN).extra_data["dns_evidence"]["spf_includes"] == []


def test_no_prior_snapshot_emits_nothing(tmp_path, monkeypatch):
    registry = _registry(tmp_path)
    _account(registry)
    _probe_returns(monkeypatch, DnsEvidence(spf_includes=["_spf.google.com"]))

    cands = TechstackSource().parse(_doc(), registry.get(DOMAIN), _meta(registry))

    assert cands == []
    assert registry.get(DOMAIN).extra_data["dns_evidence"]["spf_includes"] == ["_spf.google.com"]


def test_removal_does_not_reemit_after_snapshot_advance(tmp_path, monkeypatch):
    """Same-month dedupe: the monthly natural key re-asserts within the month,
    but the advanced snapshot means a re-parse in the SAME cycle emits nothing
    (the removal is a one-time event per include)."""
    registry = _registry(tmp_path)
    _account(
        registry,
        extra={
            "dns_evidence": {
                "observed_at": "2026-08-01",
                "mx": [],
                "spf_includes": ["mail.zendesk.com"],
                "cname": {},
            }
        },
    )
    # Probe alive (NS resolves) but zero SPF includes: the include is gone.
    _probe_returns(monkeypatch, DnsEvidence(ns=["ns1.acme.com"]))

    first = TechstackSource().parse(_doc(), registry.get(DOMAIN), _meta(registry))
    second = TechstackSource().parse(_doc(), registry.get(DOMAIN), _meta(registry))

    assert len(first) == 1
    assert first[0].natural_key.endswith(":2026-09")  # monthly key shape
    assert second == []


def test_probe_failure_never_emits_or_clobbers(tmp_path, monkeypatch):
    """A totally empty probe (dnspython missing, DNS outage) is NOT evidence:
    no delta, and the stored prior snapshot survives untouched."""
    registry = _registry(tmp_path)
    prior = {
        "observed_at": "2026-08-01",
        "mx": ["aspmx.l.google.com"],
        "spf_includes": ["_spf.google.com"],
        "cname": {},
    }
    _account(registry, extra={"dns_evidence": prior})
    _probe_returns(monkeypatch, DnsEvidence())

    cands = TechstackSource().parse(_doc(), registry.get(DOMAIN), _meta(registry))

    assert cands == []
    assert registry.get(DOMAIN).extra_data["dns_evidence"] == prior


# ── single probe per collect (parse + harvest share the cache) ───────────────


def test_parse_and_harvest_share_one_probe(tmp_path, monkeypatch):
    registry = _registry(tmp_path)
    _account(registry)
    calls: list = []
    _probe_returns(monkeypatch, DnsEvidence(spf_includes=["_spf.google.com"]), calls)

    source = TechstackSource()
    source.parse(_doc(), registry.get(DOMAIN), _meta(registry))
    source.harvest_tech(_doc(), registry.get(DOMAIN), _meta(registry))

    assert calls == [DOMAIN]  # exactly ONE probe for the whole collect


# ── persistence: tech_churn survives normalize_batch ─────────────────────────


def test_tech_churn_persists_through_normalize_batch(tmp_path, monkeypatch):
    registry = _registry(tmp_path)
    account = _account(
        registry,
        extra={
            "dns_evidence": {
                "observed_at": "2026-08-01",
                "mx": [],
                "spf_includes": ["mail.zendesk.com"],
                "cname": {},
            }
        },
    )
    # Probe alive (NS resolves) but the include is gone.
    _probe_returns(monkeypatch, DnsEvidence(ns=["ns1.acme.com"]))
    (cand,) = TechstackSource().parse(_doc(), account, _meta(registry))

    assert TechstackSource.key == "techstack"
    valid, rejected = normalize_batch(
        [cand],
        account=account,
        source=TechstackSource.key,
        taxonomy=Taxonomy.load(),
        now=f"{TODAY}T00:00:00Z",
    )

    assert rejected == []
    assert len(valid) == 1
    sig = valid[0]
    assert sig.signal_type == "tech_churn"
    assert sig.category == "technology"
    assert sig.source == "techstack"
    assert sig.signal_id == make_signal_id(
        DOMAIN, "tech_churn", f"dnschurn:{DOMAIN}:mail.zendesk.com:2026-09"
    )
