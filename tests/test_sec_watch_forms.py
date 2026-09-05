"""WATCH_FORMS contract: only forms classify_form / follow_tasks actually map."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from src.core.models import Account, Document
from src.signals.normalize import normalize_batch
from src.signals.taxonomy import Taxonomy
from src.sources.sec.collector import SecEdgarSource
from src.sources.sec.parse_8k import classify_form
from src.sources.sec.parse_submissions import Filing, parse_submissions


TODAY = date(2026, 8, 16)
FIXTURE = Path(__file__).parent / "fixtures" / "sec" / "submissions_sample.json"


def _filing(form="S-1", acc="0001") -> Filing:
    return Filing(
        accession=acc,
        form=form,
        filing_date="2026-03-03",
        report_date="2026-03-01",
        items=[],
        primary_document="a.htm",
        description=form,
        cik="0000320193",
    )


def test_watch_forms_drops_unmapped_10q_and_sc14d9():
    # classify_form maps neither 10-Q nor SC 14D9, so watching them only
    # produces fetched-then-silently-dropped rows in the fetch log.
    assert "10-Q" not in SecEdgarSource.WATCH_FORMS
    assert "SC 14D9" not in SecEdgarSource.WATCH_FORMS


def test_watch_forms_keeps_every_mapped_form():
    # Exact contract: nothing else is dropped along with the two unmapped forms.
    # "4" (Form 4) is fanned out to its primary XML doc by follow_tasks; the
    # "4/A" amendment form stays unwatched (amendment noise). SC 13D/SC 13G
    # are mapped directly to ma_target by classify_form — the /A amendments
    # stay unwatched so no fetched-then-dropped rows return.
    assert SecEdgarSource.WATCH_FORMS == {
        "8-K",
        "10-K",
        "S-1",
        "S-1/A",
        "424B4",
        "D",
        "D/A",
        "25",
        "425",
        "4",
        "SC 13D",
        "SC 13G",
    }
    assert "4/A" not in SecEdgarSource.WATCH_FORMS
    assert "SC 13D/A" not in SecEdgarSource.WATCH_FORMS
    assert "SC 13G/A" not in SecEdgarSource.WATCH_FORMS


def test_classify_form_mappings_unregressed():
    assert classify_form(_filing(form="S-1"), today=TODAY)[0].signal_type == "ipo_filing"
    assert classify_form(_filing(form="S-1/A"), today=TODAY)[0].signal_type == "ipo_filing"
    assert classify_form(_filing(form="424B4"), today=TODAY)[0].signal_type == "ipo_pricing"
    assert classify_form(_filing(form="10-K"), today=TODAY)[0].signal_type == "annual_report_10k"
    assert classify_form(_filing(form="425"), today=TODAY)[0].signal_type == "ma_acquirer"
    assert classify_form(_filing(form="25"), today=TODAY) == []


def test_classify_form_maps_sc13d_sc13g_to_ma_target():
    # 5%+ stakes (Schedule 13D/G) are M&A prelude evidence — mapped onto the
    # existing ma_target type at moderate confidence, no fanout needed.
    d = classify_form(_filing(form="SC 13D"), today=TODAY)
    assert len(d) == 1
    assert d[0].signal_type == "ma_target"
    assert d[0].confidence == 0.65
    g = classify_form(_filing(form="SC 13G"), today=TODAY)
    assert len(g) == 1
    assert g[0].signal_type == "ma_target"
    assert g[0].confidence == 0.5


def test_sc13d_g_amendments_stay_unmapped():
    assert classify_form(_filing(form="SC 13D/A"), today=TODAY) == []
    assert classify_form(_filing(form="SC 13G/A"), today=TODAY) == []


def test_sc13d_ma_target_persists_through_normalize_batch():
    # tech_churn precedent: a type missing from the taxonomy is silently
    # discarded by normalize — pin ma_target end to end for SC 13D rows.
    tax = Taxonomy.load()
    assert "ma_target" in tax._types
    cands = classify_form(_filing(form="SC 13D", acc="0009"), today=TODAY)
    valid, rejected = normalize_batch(
        cands,
        account=Account(domain="acme.com", name="Acme"),
        source="sec_edgar",
        taxonomy=tax,
        now="2026-09-05T00:00:00Z",
    )
    assert not rejected
    assert {s.signal_type for s in valid} == {"ma_target"}
    assert valid[0].domain == "acme.com"
    assert valid[0].evidence_data.get("form") == "SC 13D"


def test_formd_fanout_unregressed():
    # The D/D/A fanout still fires for exactly the D and D/A filings in the
    # submissions feed, and 8-K bodies are still followed. Form 4 rows now
    # fan out their primary XML doc too (the fixture contains 4 of them).
    _, filings = parse_submissions(FIXTURE.read_bytes())
    expected_form_d = {f.accession for f in filings if f.form in {"D", "D/A"}}
    assert expected_form_d  # fixture sanity
    src = SecEdgarSource()
    doc = Document(
        doc_id="s",
        source="sec_edgar",
        url="https://data.sec.gov/submissions/x.json",
        body=FIXTURE.read_bytes(),
        domain="acme.com",
    )
    acct = Account(domain="acme.com", cik="0000320193")
    meta = {"kind": "submissions", "today": TODAY.isoformat(), "cik": "0000320193"}
    follows = src.follow_tasks(doc, acct, meta)
    form_d = {t.meta["accession"] for t in follows if t.meta["kind"] == "form_d"}
    assert form_d == expected_form_d
    assert {t.meta["kind"] for t in follows} == {"form_d", "8k", "form4"}
