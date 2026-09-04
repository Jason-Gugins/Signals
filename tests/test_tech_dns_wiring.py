"""DNS probe evidence participates in the techstack harvest merge."""

from src.core.models import Account, Document
from src.sources.techstack.collector import TechstackSource
from src.sources.techstack.dns_probe import DnsEvidence

DOMAIN = "acme.com"


def _doc():
    return Document(
        doc_id="d",
        source="techstack",
        url=f"https://{DOMAIN}/",
        body=b"<html><body>hi</body></html>",
    )


def _fake_probe(domain):
    return DnsEvidence(cname={f"status.{domain}": "myapp.statuspage.io"})


def test_harvest_tech_merges_dns_matches(monkeypatch):
    monkeypatch.setattr("src.sources.techstack.dns_probe.probe_dns", _fake_probe)
    matches = TechstackSource().harvest_tech(
        _doc(), Account(domain=DOMAIN), {"kind": "html", "today": "2026-09-04"}
    )
    vendors = {m.vendor for m in matches}
    assert "statuspage" in vendors  # exact key from config/fingerprints.yaml dns_cname rule


def test_network_task_does_not_trigger_dns_probe(monkeypatch):
    def _must_not_probe(domain):
        raise AssertionError("DNS probe must not run on the network task")

    monkeypatch.setattr("src.sources.techstack.dns_probe.probe_dns", _must_not_probe)
    matches = TechstackSource().harvest_tech(
        _doc(), Account(domain=DOMAIN), {"kind": "network", "today": "2026-09-04"}
    )
    # Reaching here without AssertionError means the probe was skipped.
    assert isinstance(matches, list)
