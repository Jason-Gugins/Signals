"""SEC adapter parse() wiring — uses existing fixtures."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from src.core.models import Account, Document
from src.sources.sec.collector import SecEdgarSource


ACCT = Account(domain="acme.com", cik="0001234567")
TODAY = date(2026, 8, 16)


def test_submissions_emits_form_candidates_and_follow_tasks():
    body = Path("tests/fixtures/sec/submissions_sample.json").read_bytes()
    doc = Document(doc_id="s", source="sec_edgar", url="https://data.sec.gov/x", body=body, domain="acme.com")
    src = SecEdgarSource()
    meta = {"kind": "submissions", "today": TODAY.isoformat(), "cik": "0001234567"}
    cands = src.parse(doc, ACCT, meta)
    types = {c.signal_type for c in cands}
    assert {"ipo_filing", "annual_report_10k", "ipo_pricing"} <= types
    follows = src.follow_tasks(doc, ACCT, meta)
    assert follows
    assert {t.meta.get("kind") for t in follows} <= {"form_d", "8k"}
    assert all(t.meta.get("accession") for t in follows)


def test_form_d_and_8k_bodies():
    src = SecEdgarSource()
    dxml = Path("tests/fixtures/sec/form_d_primary_doc.xml").read_bytes()
    c1 = src.parse(
        Document(doc_id="d", source="sec_edgar", body=dxml),
        ACCT,
        {"kind": "form_d", "today": "2026-08-16", "filing_date": "2026-08-01", "accession": "000-1"},
    )
    assert any(c.signal_type == "funding_form_d" for c in c1)
    htm = Path("tests/fixtures/sec/8k_body.htm").read_bytes()
    c2 = src.parse(
        Document(doc_id="k", source="sec_edgar", body=htm),
        ACCT,
        {"kind": "8k", "today": "2026-08-16", "filing_date": "2026-03-01", "items": ["2.01", "5.02"]},
    )
    assert {c.signal_type for c in c2} & {"ma_acquirer", "exec_hire", "exec_departure"}
