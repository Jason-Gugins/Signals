from datetime import date
from pathlib import Path
from src.sources.techstack.fingerprint import TechMatch
from src.sources.wayback.cdx import parse_cdx, pick_snapshots, snapshot_url
from src.sources.wayback.renewal import estimate_first_seen, renewal_candidates


def test_cdx_and_pick():
    rows = parse_cdx((Path("tests/fixtures/wayback/cdx.json")).read_bytes())
    assert rows[0]["timestamp"].startswith("2020")
    picked = pick_snapshots(rows, per_year=1, max_total=3)
    assert len(picked) <= 3
    assert snapshot_url("20200101120000", "http://acme.com/").startswith("https://web.archive.org/web/20200101120000id_/")


def test_estimate_and_renewal_window():
    dated = [
        ("2024-08-01", [TechMatch("hubspot", "HubSpot", ["crm"], "mid", "s", 0.8)]),
        ("2025-08-01", [TechMatch("hubspot", "HubSpot", ["crm"], "mid", "s", 0.8)]),
    ]
    assert estimate_first_seen("hubspot", dated) == "2024-08-01"
    assert estimate_first_seen("nope", dated) is None
    today = date(2026, 7, 15)
    rows = [{"vendor": "hubspot", "first_seen_at": "2025-08-20"}]
    cands = renewal_candidates("acme.com", rows, contract_years={}, default_years=1, today=today)
    assert cands and cands[0].signal_type == "renewal_window"
    assert renewal_candidates("acme.com", [{"vendor": "x"}], contract_years={}, today=today) == []
