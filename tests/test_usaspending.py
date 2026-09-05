"""Task 9: usaspending.gov federal-contracts collector → federal_contract_award.

Fixture is a trimmed capture from the live P3 spike
(data/probe/p3_spike_api-usaspending-gov-4-post.json, 2026-09-04):
POST /api/v2/search/spending_by_award/ returns ``results`` rows keyed by
display-name fields ("Award ID", "Recipient Name", "Award Amount", ...).
"""

from datetime import date
from pathlib import Path

import yaml

import src.sources  # noqa: F401 — populates the @register registry
from src.core.models import Account, Document
from src.signals.evidence import render_evidence
from src.signals.normalize import normalize_batch
from src.signals.taxonomy import Taxonomy
from src.sources.base import SourceAdapter
from src.sources.federal import usaspending
from src.sources.federal.collector import FederalContractsSource
from src.sources.registry import get_source

FIXTURE = Path("tests/fixtures/federal/awards_search.json").read_bytes()
ACCOUNT = Account(domain="boozallen.com", name="Booz Allen Hamilton Inc")


def _parse(body=FIXTURE, account=ACCOUNT, meta=None):
    src = FederalContractsSource()
    doc = Document(
        doc_id="d1",
        source="federal_contracts",
        url="https://api.usaspending.gov/api/v2/search/spending_by_award/",
        body=body,
    )
    if meta is None:
        meta = {"today": "2026-09-05"}
    return src.parse(doc, account, meta)


# --- request-body builders ---------------------------------------------------


def test_award_search_body_shape():
    body = usaspending.award_search_body(
        "Booz Allen Hamilton", start_date="2025-09-05", end_date="2026-09-05"
    )
    assert body["filters"]["keywords"] == ["Booz Allen Hamilton"]
    assert body["filters"]["award_type_codes"] == ["A", "B", "C", "D"]
    assert body["filters"]["time_period"] == [
        {"start_date": "2025-09-05", "end_date": "2026-09-05"}
    ]
    for field in (
        "Award ID",
        "Recipient Name",
        "Start Date",
        "End Date",
        "Award Amount",
        "Description",
    ):
        assert field in body["fields"]
    assert body["limit"] == 100
    assert body["page"] == 1


def test_build_search_body_trailing_twelve_months():
    body = usaspending.build_search_body("Acme", today=date(2026, 9, 5))
    assert body["filters"]["keywords"] == ["Acme"]
    assert body["filters"]["time_period"] == [
        {"start_date": "2025-09-05", "end_date": "2026-09-05"}
    ]
    # Purity split: the no-arg default clock read lives in usaspending.py
    # (no parse() there, so the AST purity guard never scans it).
    live = usaspending.build_search_body("Acme")
    assert live["filters"]["time_period"][0]["end_date"] == date.today().isoformat()


def test_recipient_search_body_shape():
    assert usaspending.recipient_search_body("Booz Allen") == {
        "keyword": "Booz Allen",
        "limit": 10,
    }


# --- response parsing --------------------------------------------------------


def test_parse_awards_fixture_rows():
    rows = usaspending.parse_awards(FIXTURE)
    assert len(rows) == 6
    first = rows[0]
    assert first["award_id"] == "47QFCA21F0018"
    assert first["recipient_name"] == "BOOZ ALLEN HAMILTON INC"
    assert first["amount"] == 1379603153.12
    assert first["start_date"] == "2021-03-09"
    assert first["end_date"] == "2025-02-15"
    # spike capture requested no Description field — must tolerate absence
    assert first["description"] is None
    described = [r for r in rows if r["award_id"] == "47QFCA25F0123"][0]
    assert described["description"].startswith("AGILE SOFTWARE DELIVERY")
    # extra keys (internal_id, generated_internal_id, ...) are ignored
    assert set(rows[0]) == {
        "award_id",
        "recipient_name",
        "amount",
        "start_date",
        "end_date",
        "description",
    }


def test_parse_awards_accepts_dict_and_garbage():
    assert usaspending.parse_awards({"results": []}) == []
    assert usaspending.parse_awards(b"") == []
    assert usaspending.parse_awards(b"{\xff not json") == []
    # rows without an Award ID cannot form a natural key — dropped
    assert usaspending.parse_awards({"results": [{"Recipient Name": "X"}]}) == []


def test_humanize_amount():
    h = usaspending.humanize_amount
    assert h(1379603153.12) == "$1.4B"
    assert h(2_000_000_000.0) == "$2B"
    assert h(1234567.89) == "$1.2M"
    assert h(664721976.0) == "$664.7M"
    assert h(450000.0) == "$450,000"
    assert h(98765.4) == "$98,765"
    assert h(0) == "$0"
    assert h(None) is None


# --- never-guess name ladder -------------------------------------------------


def test_match_recipient_ladder():
    m = usaspending.match_recipient
    # exact (casefolded)
    assert m("BOOZ ALLEN HAMILTON INC", "Booz Allen Hamilton Inc") == "match"
    # exact beats a substring crowd — no choice to make
    assert (
        m(
            "BOOZ ALLEN HAMILTON INC",
            "Booz Allen Hamilton Inc",
            others=["BOOZ ALLEN HAMILTON INC", "BOOZ ALLEN HAMILTON INCORPORATED"],
        )
        == "match"
    )
    # unique substring, either direction (pairwise)
    assert m("BOOZ ALLEN HAMILTON INCORPORATED", "Booz Allen Hamilton Inc") == "match"
    assert m("BOOZ ALLEN HAMILTON", "Booz Allen Hamilton Inc") == "match"
    # unique substring within the response's row set
    assert (
        m(
            "BOOZ ALLEN HAMILTON ENGINEERING SERVICES, LLC",
            "Booz Allen Hamilton",
            others=["GENERAL DYNAMICS CORP", "LOCKHEED MARTIN CORP"],
        )
        == "match"
    )
    # ambiguous: needle is a substring of several distinct plausible rows
    assert (
        m(
            "BOOZ ALLEN HAMILTON ENGINEERING SERVICES, LLC",
            "Booz Allen Hamilton",
            others=["BOOZ ALLEN HAMILTON INC", "BOOZ ALLEN HAMILTON ENGINEERING SERVICES, LLC"],
        )
        == "ambiguous"
    )
    # duplicate rows (same casefolded name) are NOT distinct — no ambiguity
    assert (
        m(
            "BOOZ ALLEN HAMILTON INC",
            "Booz Allen Hamilton",
            others=["BOOZ ALLEN HAMILTON INC", "BOOZ ALLEN HAMILTON INC"],
        )
        == "match"
    )
    # no_match + empty guards (never guess on missing data)
    assert m("GENERAL DYNAMICS CORP", "Booz Allen Hamilton") == "no_match"
    assert m("BOOZ ALLEN HAMILTON INC", None) == "no_match"
    assert m("", "Booz Allen Hamilton") == "no_match"
    assert m("BOOZ ALLEN HAMILTON INC", "") == "no_match"


# --- adapter: plan -----------------------------------------------------------


def test_plan_builds_one_post_task():
    src = FederalContractsSource()
    tasks = src.plan(ACCOUNT, cursor=None)
    assert len(tasks) == 1
    task = tasks[0]
    assert task.method == "POST"
    assert task.url == "https://api.usaspending.gov/api/v2/search/spending_by_award/"
    assert task.domain == "boozallen.com"
    assert task.json_body["filters"]["keywords"] == ["Booz Allen Hamilton Inc"]
    assert task.json_body["filters"]["award_type_codes"] == ["A", "B", "C", "D"]


def test_plan_falls_back_to_domain_label_and_never_plans_empty():
    src = FederalContractsSource()
    tasks = src.plan(Account(domain="acme.io"), cursor=None)
    assert tasks[0].json_body["filters"]["keywords"] == ["acme"]
    assert src.plan(Account(domain=""), cursor=None) == []


def test_adapter_wiring_attributes():
    src = FederalContractsSource()
    assert isinstance(src, SourceAdapter)
    assert src.key == "federal_contracts"
    assert src.tier == "http"
    assert src.cadence_hours == 168
    assert src.requires == ()
    assert not getattr(src, "fanout", False)


# --- adapter: parse (end-to-end) ---------------------------------------------


def test_parse_emits_only_matched_awards():
    cands = _parse()
    assert len(cands) == 3  # 3 exact rows; ambiguous + no_match rows skipped
    by_id = {c.evidence_data["award_id"]: c for c in cands}
    assert set(by_id) == {"47QFCA21F0018", "36C10B21N10070021", "47QFCA25F0123"}
    cand = by_id["47QFCA21F0018"]
    assert cand.signal_type == "federal_contract_award"
    assert cand.natural_key == "federal:boozallen.com:47QFCA21F0018"
    assert cand.observed_at == "2026-09-05"
    assert cand.title == "BOOZ ALLEN HAMILTON INC"
    assert cand.confidence == 0.7
    ev = cand.evidence_data
    assert ev["recipient"] == "BOOZ ALLEN HAMILTON INC"
    assert ev["amount_display"] == "$1.4B"
    assert ev["period_start"] == "2021-03-09"
    assert ev["period_end"] == "2025-02-15"
    assert ev["description"] is None
    assert by_id["47QFCA25F0123"].evidence_data["amount_display"] == "$450,000"
    assert by_id["47QFCA25F0123"].evidence_data["description"].startswith(
        "AGILE SOFTWARE DELIVERY"
    )


def test_parse_skips_ambiguous_name_collisions():
    # The plan's "Metric" precedent: a short needle matching several distinct
    # recipients must emit NOTHING (candidates-only contract).
    cands = _parse(account=Account(domain="ba-holding.example", name="Booz Allen Hamilton"))
    assert cands == []


def test_parse_handles_empty_body_and_missing_today():
    assert _parse(body=None) == []
    assert _parse(body=b"{}") == []
    # no clock anywhere → observed_at None (normalize would reject; parse never guesses)
    cands = _parse(meta={})
    assert cands and all(c.observed_at is None for c in cands)


# --- registration + persistence ----------------------------------------------


def test_federal_contracts_registered_and_configured():
    assert get_source("federal_contracts") is FederalContractsSource
    entry = yaml.safe_load(Path("config/sources.yaml").read_text(encoding="utf-8"))[
        "sources"
    ]["federal_contracts"]
    assert entry == {"enabled": True, "cadence_hours": 168, "rate_per_host": 0.5}


def test_candidates_persist_through_normalize_batch():
    tax = Taxonomy.load()
    assert "federal_contract_award" in tax._types, (
        "federal_contract_award missing from signals.yaml — candidates are rejected "
        "as unknown and never persist (tech_churn precedent)"
    )
    cands = _parse()
    valid, rejected = normalize_batch(
        cands,
        account=ACCOUNT,
        source="federal_contracts",
        taxonomy=tax,
        now="2026-09-05T00:00:00Z",
    )
    assert not rejected
    assert {s.signal_type for s in valid} == {"federal_contract_award"}
    sig = valid[0]
    assert sig.category == "financial"
    assert sig.catalyst == "primary"
    assert sig.polarity == "positive"
    assert sig.source == "federal_contracts"
    assert sig.domain == "boozallen.com"
    rendered = render_evidence(sig, account=ACCOUNT, today=date(2026, 9, 5))
    assert rendered == "Federal award to BOOZ ALLEN HAMILTON INC — $1.4B (today)"
