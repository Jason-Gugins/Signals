"""Form 4 ownership-XML parsing -> insider_trade, plus fanout wiring + cap."""

from __future__ import annotations

import json
from collections import Counter
from datetime import date
from pathlib import Path

from src.core.models import Account, Document
from src.signals.normalize import normalize_batch
from src.signals.taxonomy import Taxonomy
from src.sources.sec.collector import SecEdgarSource
from src.sources.sec.parse_form4 import parse_form4

TODAY = date(2026, 9, 5)
FIXTURE = Path(__file__).parent / "fixtures" / "sec" / "form4.xml"
ACC = "0001140361-26-035636"
ACCT = Account(domain="acme.com", name="Acme Rocketry", cik="0001234567")


def _parse(body: bytes):
    return parse_form4(body, accession=ACC, today=TODAY)


def test_form4_fixture_yields_one_net_sold_candidate():
    cands = _parse(FIXTURE.read_bytes())
    assert len(cands) == 1
    c = cands[0]
    assert c.signal_type == "insider_trade"
    assert c.natural_key == f"insider:{ACC}"
    assert c.observed_at == TODAY.isoformat()
    assert c.confidence == 0.6
    ev = c.evidence_data
    # Net rule: any disposition in the filing dominates -> "sold", showing
    # the disposed total (the fixture has one acquisition AND one sale).
    assert ev["tx_type"] == "sold"
    assert ev["shares_display"] == "1,500"
    assert ev["person_name"] == "Doe Jane A"
    assert ev["security"] == "Common Stock"
    assert ev["price"] == "12.00"
    assert ev["tx_code"] == "P,S"
    assert ev["issuer"] == "Acme Rocketry, Inc."
    assert c.title == "Doe Jane A sold 1,500 Common Stock"


def test_form4_namespace_free_variant_parses_identically():
    # Live EDGAR ships BOTH namespaced and namespace-free Form 4 XML.
    stripped = FIXTURE.read_bytes().replace(
        b' xmlns="http://www.sec.gov/edgar/document/nineoxe"', b""
    )
    assert _parse(stripped) == _parse(FIXTURE.read_bytes())
    # A different (aliased) namespace must be matched just as agnostically.
    renamed = FIXTURE.read_bytes().replace(
        b"http://www.sec.gov/edgar/document/nineoxe", b"urn:example:other-ode"
    )
    assert _parse(renamed) == _parse(FIXTURE.read_bytes())


def test_form4_bought_when_only_acquisitions():
    raw = FIXTURE.read_bytes()
    tx_start = raw.index(b"<nonDerivativeTransaction>")
    tx_end = raw.index(b"</nonDerivativeTransaction>") + len(b"</nonDerivativeTransaction>")
    table_open_end = raw.index(b"<nonDerivativeTable>") + len(b"<nonDerivativeTable>")
    table_close_start = raw.index(b"</nonDerivativeTable>")
    single = (
        raw[:table_open_end]
        + raw[tx_start:tx_end]
        + raw[table_close_start:]
    )
    cands = _parse(single)
    assert len(cands) == 1
    ev = cands[0].evidence_data
    assert ev["tx_type"] == "bought"
    assert ev["shares_display"] == "500"
    assert ev["price"] == "10.50"
    assert ev["tx_code"] == "P"


def test_form4_malformed_xml_is_fail_open():
    assert _parse(b"<ownershipDocument><nonDerivativeTable><value>") == []
    assert _parse(b"") == []
    assert _parse(b"not xml at all") == []


def test_form4_missing_tables_is_fail_open():
    body = (
        b'<?xml version="1.0"?><ownershipDocument>'
        b"<issuer><issuerName>Acme Rocketry, Inc.</issuerName></issuer>"
        b"<reportingOwner><reportingOwnerId>"
        b"<rptOwnerName>Doe Jane A</rptOwnerName></reportingOwnerId>"
        b"</reportingOwner></ownershipDocument>"
    )
    assert _parse(body) == []
    # A holdings-only table (no transactions) is likewise silent.
    body += b"<nonDerivativeTable><nonDerivativeHolding><securityTitle>" \
            b"<value>Common Stock</value></securityTitle></nonDerivativeHolding>" \
            b"</nonDerivativeTable>"
    assert _parse(body) == []


def test_form4_persists_through_normalize_batch():
    # House rule (tech_churn precedent): a type missing from the taxonomy is
    # silently discarded by normalize — pin insider_trade end to end.
    tax = Taxonomy.load()
    assert "insider_trade" in tax._types
    valid, rejected = normalize_batch(
        _parse(FIXTURE.read_bytes()),
        account=ACCT,
        source="sec_edgar",
        taxonomy=tax,
        now="2026-09-05T00:00:00Z",
    )
    assert not rejected
    assert {s.signal_type for s in valid} == {"insider_trade"}
    sig = valid[0]
    assert "Doe Jane A" in (sig.evidence_data.get("person_name") or "")


def _subs_json(rows: list[tuple[str, str, str]]) -> bytes:
    """Minimal EDGAR submissions JSON: rows of (accession, form, filing_date)."""
    return json.dumps(
        {
            "cik": "0001234567",
            "filings": {
                "recent": {
                    "accessionNumber": [r[0] for r in rows],
                    "form": [r[1] for r in rows],
                    "filingDate": [r[2] for r in rows],
                    "reportDate": [None] * len(rows),
                    "items": [""] * len(rows),
                    "primaryDocument": ["form4.xml"] * len(rows),
                    "primaryDocDescription": [""] * len(rows),
                }
            },
        }
    ).encode()


def _doc(body: bytes) -> Document:
    return Document(
        doc_id="s",
        source="sec_edgar",
        url="https://data.sec.gov/submissions/CIK0001234567.json",
        body=body,
        domain="acme.com",
    )


def test_form4_fanout_and_ten_doc_cycle_cap():
    # 13 Form 4 filings (plus an amendment and an 8-K): only the 10 NEWEST
    # Form 4 primary docs are fanned out per cycle, and "4/A" never fans out.
    rows = [
        (f"0001234567-26-{i:06d}", "4", f"2026-08-{i:02d}") for i in range(1, 14)
    ]
    rows.append(("0001234567-26-000099", "4/A", "2026-08-14"))
    rows.append(("0001234567-26-000098", "8-K", "2026-08-14"))
    src = SecEdgarSource()
    follows = src.follow_tasks(_doc(_subs_json(rows)), ACCT, {"kind": "submissions"})
    kinds = Counter(t.meta["kind"] for t in follows)
    assert kinds == {"form4": 10, "8k": 1}
    form4 = [t for t in follows if t.meta["kind"] == "form4"]
    # Newest-first: the cap must keep the freshest filings, drop the oldest.
    newest = {f"0001234567-26-{i:06d}" for i in range(4, 14)}
    assert {t.meta["accession"] for t in form4} == newest
    for t in form4:
        assert t.url.endswith("form4.xml")
        assert t.meta["filing_date"].startswith("2026-08-")
        assert t.source == "sec_edgar"


def test_form4_submissions_rows_emit_nothing_without_the_xml_doc():
    # The submissions row itself maps to no signal (classify_form is silent
    # for "4"); the signal only arrives when the fanned-out XML doc parses.
    src = SecEdgarSource()
    cands = src.parse(_doc(_subs_json([("0001234567-26-000001", "4", "2026-08-01")])), ACCT, {
        "kind": "submissions",
        "today": TODAY.isoformat(),
        "cik": "0001234567",
    })
    assert cands == []


def test_collector_parses_form4_doc_via_kind_meta():
    src = SecEdgarSource()
    cands = src.parse(
        Document(
            doc_id="f4",
            source="sec_edgar",
            url="https://www.sec.gov/Archives/edgar/data/1234567/000123456726000001/form4.xml",
            body=FIXTURE.read_bytes(),
            domain="acme.com",
        ),
        ACCT,
        {"kind": "form4", "today": "2026-09-05", "accession": ACC, "cik": "0001234567"},
    )
    assert len(cands) == 1
    assert cands[0].signal_type == "insider_trade"
    assert cands[0].natural_key == f"insider:{ACC}"
    assert cands[0].url.endswith("form4.xml")
