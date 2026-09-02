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


# --- P3 Batch 2 hardening ---------------------------------------------------

from src.sources.warn.states.fl import FlWarn as _FlWarn  # noqa: E402
from src.sources.warn.states.oh import OhWarn as _OhWarn  # noqa: E402


def _fl_row(cell: str) -> bytes:
    return (
        "<table><tr><th>Company</th><th>Date</th></tr>"
        f"<tr><td>{cell}</td><td>01/15/2026</td></tr></table>"
    ).encode()


def test_fl_comma_in_company_right_anchored_split():
    """Company names containing commas survive: split is right-anchored."""
    notices = _FlWarn().parse(
        _fl_row("Acme, Inc, 123 Main St, Columbus"))
    assert len(notices) == 1
    assert notices[0].company_raw == "Acme, Inc"
    assert notices[0].location == "Columbus"


def test_fl_suite_number_stripped_from_location():
    """Suite/unit segments never leak into the location (right-anchored:
    'Suite 200' lands in the street slot; location is the city only)."""
    notices = _FlWarn().parse(
        _fl_row("Gulf Widgets, 456 Oak Ave, Suite 200, Tampa"))
    assert len(notices) == 1
    assert "Suite 200" not in (notices[0].location or "")
    assert notices[0].location == "Tampa"


def test_oh_relative_href_resolved_with_urljoin():
    notices = _OhWarn().parse(
        b"<html><body><a href='/warn/acme.pdf'>Acme Corp - Franklin County</a>"
        b"</body></html>")
    assert len(notices) == 1
    assert notices[0].url == "https://jfs.ohio.gov/warn/acme.pdf"


def test_nj_lazy_import_degrades_to_noop_without_openpyxl(monkeypatch):
    """Missing openpyxl degrades NJ to a no-op; other imports unaffected."""
    import sys

    monkeypatch.setitem(sys.modules, "openpyxl", None)
    assert NjWarn().parse(b"not-a-workbook") == []


def test_nj_header_scanned_beyond_first_row():
    """Header row is located by scanning the first rows, not assumed row 1."""
    from openpyxl import Workbook
    from io import BytesIO

    wb = Workbook()
    ws = wb.active
    ws.append(["Confidential report"])  # decoy preamble row
    ws.append([None])  # blank row
    ws.append(["Company Name", "Notice Date", "Effective Date",
               "Number Affected", "City", "Reason"])
    ws.append(["Beta LLC", "2026-02-01", "2026-04-01", 40, "Newark", "Closure"])
    buf = BytesIO()
    wb.save(buf)
    notices = NjWarn().parse(buf.getvalue())
    assert len(notices) == 1
    assert notices[0].company_raw == "Beta LLC"
    assert notices[0].notice_date == "2026-02-01"
    assert notices[0].affected == 40
    assert notices[0].location == "Newark"
