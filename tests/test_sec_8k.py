"""Tests for SEC 8-K / S-1 / 10-K classification."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from src.core.models import Account
from src.signals.normalize import normalize_batch
from src.signals.taxonomy import Taxonomy
from src.sources.sec.parse_8k import classify_8k, classify_form, extract_text
from src.sources.sec.parse_submissions import Filing


TODAY = date(2026, 8, 16)
FIXTURE = Path(__file__).parent / "fixtures" / "sec" / "8k_body.htm"


def _filing(form="8-K", items=None, acc="0001") -> Filing:
    return Filing(
        accession=acc,
        form=form,
        filing_date="2026-03-03",
        report_date="2026-03-01",
        items=items or [],
        primary_document="a.htm",
        description=form,
        cik="0000320193",
    )


def test_item_only_each_mapped_item():
    mapping = {
        "1.01": "ma_acquirer",
        "2.01": "ma_acquirer",
        "2.05": "layoff",
        "2.06": "earnings_warning",
        "5.02": "exec_hire",
    }
    for item, typ in mapping.items():
        cands = classify_8k(_filing(items=[item]), None, today=TODAY)
        if typ is None:
            assert cands == []
        else:
            assert any(c.signal_type == typ for c in cands), (item, cands)


def test_unmapped_items_nothing():
    assert classify_8k(_filing(items=["7.01", "8.01", "9.01"]), None, today=TODAY) == []


def test_502_appointment_vs_resignation_vs_both():
    hire = classify_8k(_filing(items=["5.02"]), "The Board appointed Ms. Jane Doe as CRO.", today=TODAY)
    assert {c.signal_type for c in hire} == {"exec_hire"}
    leave = classify_8k(_filing(items=["5.02"]), "John Smith resigned and will step down.", today=TODAY)
    assert {c.signal_type for c in leave} == {"exec_departure"}
    both = classify_8k(_filing(items=["5.02"]), FIXTURE.read_text(encoding="utf-8"), today=TODAY)
    assert {"exec_hire", "exec_departure"} <= {c.signal_type for c in both}


def test_name_role_and_acquired_company_on_fixture():
    text = extract_text(FIXTURE.read_bytes())
    cands = classify_8k(_filing(items=["2.01", "5.02"]), text, today=TODAY)
    acq = next(c for c in cands if c.signal_type == "ma_acquirer")
    assert "Widget" in (acq.evidence_data.get("acquired_company") or "")
    hire = next(c for c in cands if c.signal_type == "exec_hire")
    assert hire.evidence_data.get("person_name")
    assert hire.evidence_data.get("new_role")


def test_classify_form_s1_424_10k():
    assert classify_form(_filing(form="S-1"), today=TODAY)[0].signal_type == "ipo_filing"
    assert classify_form(_filing(form="S-1/A"), today=TODAY)[0].signal_type == "ipo_filing"
    assert classify_form(_filing(form="424B4"), today=TODAY)[0].signal_type == "ipo_pricing"
    assert classify_form(_filing(form="10-K"), today=TODAY)[0].signal_type == "annual_report_10k"
    assert classify_form(_filing(form="25"), today=TODAY) == []
    assert classify_form(_filing(form="425"), today=TODAY)[0].signal_type == "ma_acquirer"


def test_extract_text_strips_and_caps():
    text = extract_text(FIXTURE.read_bytes())
    assert "<p>" not in text
    assert "Widget Labs" in text
    huge = extract_text(b"<html>" + (b"word " * 80_000) + b"</html>")
    assert len(huge) <= 200_000


def test_disposition_is_not_ma_acquirer():
    # Item 2.01 covers BOTH acquisition AND disposition.
    # A seller completing a divestiture must NOT be labeled ma_acquirer.
    text = "On March 10, 2026, the Company completed the sale of its Widget division to Buyer Co."
    cands = classify_8k(_filing(items=["2.01"]), text, today=TODAY)
    assert not any(c.signal_type == "ma_acquirer" for c in cands), cands


def test_acquisition_still_ma_acquirer():
    text = "On March 1, 2026, the Company completed the acquisition of Widget Labs, Inc."
    cands = classify_8k(_filing(items=["2.01"]), text, today=TODAY)
    assert any(c.signal_type == "ma_acquirer" for c in cands)


def test_ma_target_emitted_for_acquired_company_on_acquisition():
    text = "On March 1, 2026, the Company completed the acquisition of Widget Labs, Inc."
    cands = classify_8k(_filing(items=["2.01"]), text, today=TODAY)
    assert any(c.signal_type == "ma_acquirer" for c in cands)
    target = next((c for c in cands if c.signal_type == "ma_target"), None)
    assert target is not None
    assert "Widget" in (target.evidence_data.get("acquired_company") or "")


def test_ma_target_not_emitted_on_disposition():
    text = "The Company completed the sale of its Widget division to Buyer Co."
    cands = classify_8k(_filing(items=["2.01"]), text, today=TODAY)
    assert not any(c.signal_type == "ma_target" for c in cands)


def test_delisting_and_nonreliance_emit_earnings_warning():
    # 3.01 delisting notice, 4.02 non-reliance on prior financials — both
    # real material negative events; both map to earnings_warning at low conf.
    for item in ["3.01", "4.02"]:
        cands = classify_8k(_filing(items=[item]), None, today=TODAY)
        assert any(c.signal_type == "earnings_warning" for c in cands), (item, cands)
        c = next(c for c in cands if c.signal_type == "earnings_warning")
        assert c.confidence <= 0.7, (item, c.confidence)


MA_DISP_FIXTURE = Path(__file__).parent / "fixtures" / "sec" / "8k_ma_disposition.htm"


def test_disposition_fixture_no_ma_signal():
    text = extract_text(MA_DISP_FIXTURE.read_bytes())
    cands = classify_8k(_filing(items=["2.01"]), text, today=TODAY)
    assert not any(c.signal_type in {"ma_acquirer", "ma_target"} for c in cands)


def test_item_103_bankruptcy_signal():
    # Item 1.03 (bankruptcy/receivership) — the strongest negative signal in
    # the form — maps to bankruptcy_signal at high confidence.
    cands = classify_8k(_filing(items=["1.03"]), None, today=TODAY)
    c = next(c for c in cands if c.signal_type == "bankruptcy_signal")
    assert c.confidence == 0.9
    assert c.natural_key == "0001:1.03"
    assert c.evidence_data.get("item") == "1.03"


def test_item_103_chapter_captured_from_body():
    # Chapter of the bankruptcy code (7 = liquidation, 11 = reorganization)
    # is captured into evidence_data when the body states it.
    text = (
        "On March 1, 2026, the Company filed a voluntary petition for relief "
        "under chapter 11 of the United States Bankruptcy Code."
    )
    cands = classify_8k(_filing(items=["1.03"]), text, today=TODAY)
    c = next(c for c in cands if c.signal_type == "bankruptcy_signal")
    assert c.evidence_data.get("chapter") == "11"


def test_item_102_contract_terminated():
    # Item 1.02 (termination of a material agreement) — incumbent-displacement
    # evidence — maps to contract_terminated.
    cands = classify_8k(_filing(items=["1.02"]), None, today=TODAY)
    c = next(c for c in cands if c.signal_type == "contract_terminated")
    assert c.confidence == 0.7
    assert c.natural_key == "0001:1.02"
    assert c.evidence_data.get("item") == "1.02"


def test_neither_102_nor_103_emits_new_types():
    # Existing behavior unchanged: an 8-K with neither item produces no new
    # candidate types.
    cands = classify_8k(_filing(items=["7.01", "8.01", "9.01"]), None, today=TODAY)
    assert not any(
        c.signal_type in {"bankruptcy_signal", "contract_terminated"} for c in cands
    )


def test_new_types_persist_through_normalize_batch():
    # tech_churn precedent: a type missing from the taxonomy is silently
    # discarded by normalize. Pin both new types end-to-end.
    tax = Taxonomy.load()
    for typ in ("bankruptcy_signal", "contract_terminated"):
        assert typ in tax._types, (
            f"{typ} missing from signals.yaml — 8-K candidates are rejected "
            "as unknown and never persist"
        )
    cands = classify_8k(_filing(items=["1.02", "1.03"]), None, today=TODAY)
    assert {c.signal_type for c in cands} == {"bankruptcy_signal", "contract_terminated"}
    valid, rejected = normalize_batch(
        cands,
        account=Account(domain="acme.com", name="Acme"),
        source="sec_edgar",
        taxonomy=tax,
        now="2026-08-16T00:00:00Z",
    )
    assert not rejected
    assert {"bankruptcy_signal", "contract_terminated"} <= {s.signal_type for s in valid}
