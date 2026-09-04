from datetime import date
from pathlib import Path
from src.core.db import Database
from src.core.models import Account
from src.identity.registry import AccountRegistry
from src.sources.warn import match_notices, parse_affected, warn_to_candidate
from src.sources.warn.states.ca import CaWarn
from src.sources.warn.states.ny import NyWarn

FIX = Path(__file__).parent / "fixtures" / "warn"


def test_parse_ny_and_dates_and_affected():
    notices = NyWarn().parse((FIX / "ny.html").read_bytes())
    assert len(notices) == 2
    assert notices[0].company_raw == "Acme Corp."
    assert notices[0].notice_date == "2026-01-05"
    assert notices[0].affected == 1234
    assert notices[1].affected is None


def test_parse_ca():
    notices = CaWarn().parse((FIX / "ca.csv").read_bytes())
    assert notices[0].state == "CA"
    assert notices[0].affected == 50


def test_parse_affected():
    assert parse_affected("1,234") == 1234
    assert parse_affected("N/A") is None


def test_match_notices_conservative(tmp_path):
    db = Database(tmp_path / "s.db")
    reg = AccountRegistry(db)
    reg.upsert(Account(domain="acme.com", name="Acme Corp"))
    notices = NyWarn().parse((FIX / "ny.html").read_bytes()) + CaWarn().parse((FIX / "ca.csv").read_bytes())
    unmatched = tmp_path / "warn_unmatched.csv"
    pairs = match_notices(notices, reg, min_similarity=0.92, unmatched_path=str(unmatched))
    domains = {d for _, d in pairs}
    names = {n.company_raw for n, _ in pairs}
    assert "acme.com" in domains
    assert "Acme Roofing LLC" not in names
    assert unmatched.exists()
    cand = warn_to_candidate(notices[0], today=date(2026, 8, 16))
    assert cand.signal_type == "layoff" and cand.confidence == 0.95


def test_ca_parse_list_typed_cells():
    """Live CA pages sometimes return list-typed CSV cells — must not crash."""
    from src.sources.warn.states.ca import CaWarn
    import csv, io
    body = b'Company,Notice Date,Employees\n["Acme Corp"],2026-08-01,[42]\n'
    notices = CaWarn().parse(body)
    assert len(notices) == 1
    assert "Acme" in notices[0].company_raw
