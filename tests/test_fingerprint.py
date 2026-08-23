from datetime import date
from src.core.db import Database
from src.sources.techstack.collector import tech_to_candidates, upsert_technologies
from src.sources.techstack.fingerprint import TechMatch, extract_network_evidence
import yaml
from pathlib import Path

RULES = yaml.safe_load(Path("config/fingerprints.yaml").read_text(encoding="utf-8"))
NET_FIX = Path("tests/fixtures/techstack/network_scanner.dev.json")


def test_extract_network_evidence_from_fixture():
    ev = extract_network_evidence(NET_FIX.read_bytes())
    assert "cdn.prod.website-files.com" in ev.hosts
    assert ev.page_url
    assert all("?" not in u for u in ev.urls)


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
