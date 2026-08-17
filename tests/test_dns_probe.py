import yaml
from pathlib import Path
from src.sources.techstack.dns_probe import DnsEvidence, dns_evidence_to_matches, parse_spf, probe_dns
from src.sources.techstack.fingerprint import TechMatch, extract_http_evidence, merge_matches

RULES = yaml.safe_load(Path("config/fingerprints.yaml").read_text(encoding="utf-8"))


def test_parse_spf():
    assert "hubspotemail.net" in parse_spf(["v=spf1 include:hubspotemail.net -all"])
    assert parse_spf(["hello"]) == []
    assert parse_spf(['"v=spf1 include:a include:b ~all"']) == ["a", "b"]
    assert parse_spf(["v=spf1 +mx -all"]) == []
    assert parse_spf(["v=spf1 include:_spf.google.com"]) == ["_spf.google.com"]
    assert parse_spf(["not spf include:x"]) == []


def test_dns_matches():
    ev = DnsEvidence(mx=["aspmx.l.google.com"], cname={"status.acme.com": "acme.statuspage.io"}, spf_includes=["mktomail.com"])
    vendors = {m.vendor for m in dns_evidence_to_matches(ev, RULES)}
    assert "google_workspace" in vendors
    assert "statuspage" in vendors
    assert "marketo" in vendors
    assert dns_evidence_to_matches(DnsEvidence(), RULES) == []


def test_probe_dns_fake_resolver():
    class R:
        def resolve(self, name, rtype):
            if rtype == "MX":
                class A:
                    exchange = "aspmx.l.google.com."
                return [A()]
            raise Exception("NXDOMAIN")
    ev = probe_dns("acme.com", resolver=R())
    assert ev.mx == ["aspmx.l.google.com"]


def test_http_extract_and_merge():
    html = Path("tests/fixtures/techstack/homepage.html").read_bytes()
    ev = extract_http_evidence(html, {"Set-Cookie": "foo=1"}, "https://acme.com/")
    assert any("hs-scripts" in s for s in ev.script_srcs)
    assert "analytics" in ev.inline_globals or any("analytics" in g for g in ev.inline_globals)
    a = [TechMatch("hubspot", "HubSpot", ["crm"], "mid", "script", 0.6)]
    b = [TechMatch("hubspot", "HubSpot", ["crm"], "mid", "script", 0.9)]
    merged = merge_matches(a, b)
    assert len(merged) == 1 and merged[0].confidence == 0.9
