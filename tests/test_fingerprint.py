from datetime import date
from src.core.db import Database
from src.sources.techstack.collector import tech_to_candidates, upsert_technologies
from src.sources.techstack.fingerprint import (
    TechMatch,
    dynamic_matches,
    extract_network_evidence,
    match_fingerprints,
    observed_hosts,
)
import yaml
from pathlib import Path

RULES = yaml.safe_load(Path("config/fingerprints.yaml").read_text(encoding="utf-8"))
NET_FIX = Path("tests/fixtures/techstack/network_scanner.dev.json")


def test_extract_network_evidence_from_fixture():
    ev = extract_network_evidence(NET_FIX.read_bytes())
    assert "cdn.prod.website-files.com" in ev.hosts
    assert ev.page_url
    assert all("?" not in u for u in ev.urls)


def test_observed_hosts_drops_first_party():
    ev = extract_network_evidence(NET_FIX.read_bytes())
    hosts = observed_hosts(ev, domain="scanner.dev")
    assert "scanner.dev" not in hosts
    assert "cdn.prod.website-files.com" in hosts
    assert "js.hsforms.net" in hosts


def test_dynamic_matches_are_unknown_tier():
    ev = extract_network_evidence(NET_FIX.read_bytes())
    ms = dynamic_matches(ev, domain="scanner.dev")
    assert any(m.vendor == "host:cdn-cookieyes.com" for m in ms)
    assert all(m.tier == "unknown" for m in ms)
    assert not any(m.vendor.startswith("host:scanner.dev") for m in ms)


def test_named_majority_from_scanner_fixture():
    from src.sources.techstack.fingerprint import load_fingerprint_rules

    rules = load_fingerprint_rules()
    ev = extract_network_evidence(NET_FIX.read_bytes())
    named = {m.vendor for m in match_fingerprints(ev, rules)}
    assert {"webflow", "hubspot", "gtm", "google_analytics", "meta_pixel", "cookieyes", "vector"} <= named


def test_match_network_hosts_webflow_from_fixture():
    ev = extract_network_evidence(NET_FIX.read_bytes())
    hits = match_fingerprints(ev, RULES)
    assert any(m.vendor == "webflow" and m.evidence == "network_host" for m in hits)


def test_network_host_suffix_does_not_hit_workforce():
    from src.sources.techstack.fingerprint import NetworkEvidence

    ev = NetworkEvidence(page_url="https://x.com/", hosts=("workforce.com",), urls=())
    rules = {"vendors": {"salesforce": {"display": "Salesforce", "match": {"network_host": ["force.com"]}}}}
    assert match_fingerprints(ev, rules) == []


def test_upsert_and_disappear(tmp_path):
    db = Database(tmp_path / "s.db")
    m = TechMatch("hubspot", "HubSpot", ["crm"], "mid", "script", 0.8)
    new, gone = upsert_technologies(db, "acme.com", [m], now="2026-08-01")
    assert new == ["hubspot"] and gone == []
    new, gone = upsert_technologies(db, "acme.com", [], now="2026-08-08")
    assert gone == []
    new, gone = upsert_technologies(db, "acme.com", [], now="2026-08-15")
    assert "hubspot" in gone


def test_high_ticket_once_per_year():
    rows = [{"vendor": "salesforce", "tier": "enterprise", "first_seen_at": "2026-01-01"}]
    c1 = tech_to_candidates("acme.com", ["salesforce"], [], rows, RULES, [], today=date(2026, 8, 16))
    types = [c.signal_type for c in c1]
    assert "tech_install_new" in types and "high_ticket_tech" in types
    # same natural_key year means store would dedupe
    assert [c.natural_key for c in c1 if c.signal_type == "high_ticket_tech"][0].endswith("2026")
