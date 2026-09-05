"""Status-page incident polling: DNS gate -> statuspage index.json -> competitor_outage.

Pins the Task 6 seam: when the techstack DNS probe reveals
status.<domain> -> *.statuspage.io, plan() appends one extra FetchTask against
the statuspage index.json and parse() turns unresolved incidents into
competitor_outage candidates (first-party outage evidence). A regression to
unknown-signal-type or missing taxonomy entry would silently discard every
outage candidate — the persistence test below pins ingestion (tech_churn
precedent).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.models import Account, Document
from src.signals.normalize import normalize_batch
from src.signals.taxonomy import Taxonomy
from src.sources.techstack import dns_probe
from src.sources.techstack.collector import TechstackSource
from src.sources.techstack.dns_probe import statuspage_target

FIXTURE = Path("tests/fixtures/techstack/statuspage_index.json")


class _FakeResolver:
    """Canned resolver: resolve(name, rtype) -> answers for mapped CNAMEs, raise otherwise."""

    def __init__(self, mapping: dict[str, str]):
        self._mapping = mapping

    def resolve(self, name, rtype):
        if rtype != "CNAME" or name not in self._mapping:
            raise Exception("NXDOMAIN")
        class _Ans:
            def __init__(self, target: str):
                self._target = target

            def __str__(self) -> str:
                return self._target

        return [_Ans(self._mapping[name])]


# --- statuspage_target: the DNS gate ----------------------------------------


def test_statuspage_target_returns_statuspage_host():
    r = _FakeResolver({"status.acme.com": "acme.statuspage.io."})
    assert statuspage_target("acme.com", resolver=r) == "acme.statuspage.io"


def test_statuspage_target_non_statuspage_cname_returns_none():
    r = _FakeResolver({"status.acme.com": "uptime.othervendor.net"})
    assert statuspage_target("acme.com", resolver=r) is None


def test_statuspage_target_nxdomain_raises_to_none():
    class _Boom:
        def resolve(self, name, rtype):
            raise Exception("NXDOMAIN")

    assert statuspage_target("acme.com", resolver=_Boom()) is None


# --- plan(): the gate emits exactly one extra task ---------------------------


def test_plan_appends_status_task_only_when_gate_passes(monkeypatch):
    monkeypatch.setattr(dns_probe, "statuspage_target", lambda domain, **kw: "acme.statuspage.io")
    tasks = TechstackSource().plan(Account(domain="acme.com"), None)
    kinds = [(t.meta or {}).get("kind") for t in tasks]
    assert kinds == ["html", "network", "statuspage"]
    st = tasks[2]
    assert st.url == "https://acme.statuspage.io/index.json"
    assert st.domain == "acme.com"
    # the runner injects meta["today"] at fetch time; plan must not set it
    assert "today" not in (st.meta or {})


def test_plan_emits_no_status_task_when_gate_fails(monkeypatch):
    monkeypatch.setattr(dns_probe, "statuspage_target", lambda domain, **kw: None)
    tasks = TechstackSource().plan(Account(domain="acme.com"), None)
    assert [(t.meta or {}).get("kind") for t in tasks] == ["html", "network"]


def test_plan_gate_failure_is_fail_open(monkeypatch):
    def _boom(domain, **kw):
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(dns_probe, "statuspage_target", _boom)
    tasks = TechstackSource().plan(Account(domain="acme.com"), None)
    assert [(t.meta or {}).get("kind") for t in tasks] == ["html", "network"]


# --- parse(): incidents -> competitor_outage ---------------------------------


def _status_doc(body: bytes) -> Document:
    return Document(
        doc_id="s",
        source="techstack",
        url="https://acme.statuspage.io/index.json",
        domain="acme.com",
        content_type="application/json",
        body=body,
    )


def test_parse_statuspage_emits_one_candidate_for_unresolved_incident():
    cands = TechstackSource().parse(
        _status_doc(FIXTURE.read_bytes()),
        Account(domain="acme.com"),
        {"kind": "statuspage", "today": "2026-09-05"},
    )
    assert len(cands) == 1
    c = cands[0]
    assert c.signal_type == "competitor_outage"
    assert c.natural_key == "outage:acme.com:inc_investigating_01"
    assert c.observed_at == "2026-09-05"
    assert c.title == "Elevated API errors"
    assert c.confidence == 0.85
    assert c.evidence_data == {
        "incident": "Elevated API errors",
        "status": "investigating",
        "impact": "major",
        "started_at": "2026-09-04T10:00:00.000Z",
        "url": "https://stspg.io/p1",
    }


def test_parse_statuspage_malformed_json_returns_empty_without_raise():
    cands = TechstackSource().parse(
        _status_doc(b"<html>not json</html>"),
        Account(domain="acme.com"),
        {"kind": "statuspage", "today": "2026-09-05"},
    )
    assert cands == []


@pytest.mark.parametrize("body", [b"{}", b'{"incidents": null}', b'{"incidents": [{}]}'])
def test_parse_statuspage_missing_keys_fail_open(body):
    cands = TechstackSource().parse(
        _status_doc(body),
        Account(domain="acme.com"),
        {"kind": "statuspage", "today": "2026-09-05"},
    )
    assert cands == []


def test_competitor_outage_persists_through_normalize():
    """House contract: the type must survive normalize_batch (tech_churn precedent)."""
    tax = Taxonomy.load()
    assert "competitor_outage" in tax._types
    cands = TechstackSource().parse(
        _status_doc(FIXTURE.read_bytes()),
        Account(domain="acme.com", name="Acme"),
        {"kind": "statuspage", "today": "2026-09-05"},
    )
    valid, rejected = normalize_batch(
        cands,
        account=Account(domain="acme.com", name="Acme"),
        source="techstack",
        taxonomy=tax,
        now="2026-09-05T00:00:00Z",
    )
    assert len(valid) == 1
    assert not rejected
