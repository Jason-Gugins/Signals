"""DNS probe evidence participates in the techstack harvest merge."""

from src.core.models import Account, Document
from src.sources.techstack.collector import TechstackSource
from src.sources.techstack.dns_probe import DnsEvidence, dns_evidence_to_matches
from src.sources.techstack.fingerprint import load_fingerprint_rules

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


def test_dns_cname_names_instatus_status_page():
    # Task 6 never-guess probe 2026-10-03: Instatus hosts customer status
    # pages behind cname.instatus.com (their custom-domain docs) — observed
    # live: status.basedash.com -> cname.instatus.com.
    ev = DnsEvidence(cname={"status.acme.com": "cname.instatus.com"})
    matches = dns_evidence_to_matches(ev, load_fingerprint_rules())
    assert "instatus" in {m.vendor for m in matches}


def test_dns_cname_names_better_stack_status_page():
    # Task 6 never-guess probe 2026-10-03: Better Stack hosts customer status
    # pages behind *.betteruptime.com (their docs say CNAME to
    # status.betteruptime.com) — observed live: status.raycast.com,
    # status.trigger.dev, status.hookdeck.com -> statuspage.betteruptime.com.
    ev = DnsEvidence(cname={"status.acme.com": "statuspage.betteruptime.com"})
    matches = dns_evidence_to_matches(ev, load_fingerprint_rules())
    assert "better_stack" in {m.vendor for m in matches}


def test_dns_cname_names_incident_io_status_page():
    # Task 6 never-guess probe 2026-10-03: incident.io hosts customer status
    # pages behind statuspage.incident.io — observed live: status.plex.tv,
    # status.clickhouse.com, status.dub.co, status.mintlify.com,
    # status.clerk.com, status.unkey.com -> statuspage.incident.io.
    ev = DnsEvidence(cname={"status.acme.com": "statuspage.incident.io"})
    matches = dns_evidence_to_matches(ev, load_fingerprint_rules())
    assert "incident_io" in {m.vendor for m in matches}


def test_dns_cname_statuspage_custom_domain_target_names_statuspage():
    # Task 6 never-guess probe 2026-10-03: Atlassian Statuspage custom domains
    # CNAME to <hash>.stspg-customer.com — observed live: status.render.com,
    # status.supabase.com, status.sentry.io, www.githubstatus.com,
    # status.cursor.com, status.1password.com, status.warp.dev.
    ev = DnsEvidence(cname={"status.acme.com": "kctbh9vrtdwd.stspg-customer.com"})
    matches = dns_evidence_to_matches(ev, load_fingerprint_rules())
    assert "statuspage" in {m.vendor for m in matches}


def test_dns_cname_unconfirmed_host_stays_unnamed():
    # Negative arm: a status CNAME to a host with no confirmed vendor suffix
    # yields NO named match — it stays an unnamed host: inventory row (the
    # collector's dynamic-match fallback), never an invented vendor.
    ev = DnsEvidence(cname={"status.acme.com": "status.acme-internal.example.net"})
    assert dns_evidence_to_matches(ev, load_fingerprint_rules()) == []
