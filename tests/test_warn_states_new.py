"""Tests for NJ (XLSX), FL (HTML table), OH (link-list) WARN parsers."""

from pathlib import Path

from src.sources.warn.states.fl import FlWarn
from src.sources.warn.states.nj import NjWarn
from src.sources.warn.states.oh import OhWarn

FIX = Path(__file__).parent / "fixtures" / "warn"


# --- NJ: openpyxl XLSX archive -------------------------------------------

def test_nj_parse_happy_path():
    notices = NjWarn().parse((FIX / "nj.xlsx").read_bytes())
    assert len(notices) == 2
    assert notices[0].company_raw == "Acme Manufacturing"
    assert notices[0].state == "NJ"
    assert notices[0].notice_date == "2026-01-10"
    assert notices[0].effective_date == "2026-03-01"
    assert notices[0].affected == 250
    assert notices[0].location == "Trenton"
    assert notices[1].affected == 85


def test_nj_parse_empty():
    assert NjWarn().parse(b"") == []


# --- FL: reactwarn paginated HTML table ----------------------------------

def test_fl_parse_happy_path():
    notices = FlWarn().parse((FIX / "fl.html").read_bytes())
    assert len(notices) == 2
    n = notices[0]
    assert n.company_raw == "Gulf Coast Widgets"
    assert n.state == "FL"
    assert n.location == "Pensacola"
    # company cell is concatenated "Company, Street, City" — street must not leak
    assert "123 Main St" not in n.company_raw
    assert n.notice_date == "2026-01-15"
    assert n.effective_date == "2026-02-28"
    assert n.affected == 120
    assert n.reason == "Manufacturing"


def test_fl_parse_skips_rows_missing_company():
    notices = FlWarn().parse((FIX / "fl.html").read_bytes())
    # fixture includes a row with only address text (no company) — it must be skipped
    assert all(n.company_raw for n in notices)


def test_fl_parse_empty():
    assert FlWarn().parse(b"") == []


# --- OH: listing-only link-list HTML -------------------------------------

def test_oh_parse_listing_only():
    notices = OhWarn().parse((FIX / "oh.html").read_bytes())
    assert len(notices) == 2
    n = notices[0]
    assert n.company_raw == "Buckeye Steel Works"
    assert n.location == "Franklin"
    assert n.url == "https://jfs.ohio.gov/warn/buckeye-steel.pdf"
    assert n.state == "OH"
    # listing-only: detail fields deferred (PDF detail parsing)
    assert n.notice_date is None
    assert n.effective_date is None
    assert n.affected is None


def test_oh_parse_skips_rows_missing_company():
    notices = OhWarn().parse((FIX / "oh.html").read_bytes())
    assert all(n.company_raw for n in notices)


def test_oh_parse_empty():
    assert OhWarn().parse(b"") == []
