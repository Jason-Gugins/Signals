"""Task 11 — BBB profile parser + adapter tests (fixture: trimmed real Avalara capture)."""

from __future__ import annotations

from pathlib import Path

from src.core.db import Database
from src.core.models import Account, Document
from src.identity.registry import AccountRegistry
from src.sources.bbb import BbbProfileSource, parse_bbb_profile

FIXTURE = Path(__file__).parent / "fixtures" / "bbb" / "bbb_avalara_profile.html"
BBB_URL = "https://www.bbb.org/us/wa/seattle/profile/computer-software-developers/avalara-inc-1296-22018273"


def _html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def _doc() -> Document:
    return Document(
        doc_id="bbb-test-doc",
        source="bbb_profile",
        url=BBB_URL,
        body=_html().encode("utf-8"),
    )


# ---------------------------------------------------------------- parser ----

def test_parse_extracts_identity_and_facts_from_real_fixture():
    facts = parse_bbb_profile(_html())

    assert facts["business_name"] == "Avalara Inc"
    assert facts["entity_type"] == "Corporation"
    assert facts["file_opened"] == "6/3/2005"
    assert facts["accredited"] is True
    assert facts["bbb_rating"] == "A+"


def test_parse_extracts_alternate_names_in_order():
    facts = parse_bbb_profile(_html())

    assert facts["alternate_names"] == [
        "Avalara AvaTax",
        "Track1099",
        "Avalara",
        "Avalara.com",
    ]


def test_parse_is_defensive_on_empty_and_garbage():
    empty = parse_bbb_profile("")
    assert empty["business_name"] is None
    assert empty["alternate_names"] == []
    assert empty["accredited"] is False

    junk = parse_bbb_profile("<html><body>not a bbb page</body></html>")
    assert junk["business_name"] is None
    assert junk["entity_type"] is None
    assert junk["bbb_rating"] is None


# --------------------------------------------------------------- adapter ----

def _account(**kw):
    defaults = dict(
        domain="avalara.com",
        name="Avalara Inc",
        extra_data={"bbb_url": BBB_URL},
    )
    defaults.update(kw)
    return Account(**defaults)


def test_plan_emits_task_when_bbb_url_seeded():
    tasks = BbbProfileSource().plan(_account(), cursor=None)

    assert len(tasks) == 1
    assert tasks[0].source == "bbb_profile"
    assert tasks[0].url == BBB_URL
    assert tasks[0].domain == "avalara.com"


def test_plan_empty_without_bbb_url():
    account = _account(extra_data={})
    assert BbbProfileSource().plan(account, cursor=None) == []

    account = _account(extra_data={"bbb_url": ""})
    assert BbbProfileSource().plan(account, cursor=None) == []


def test_parse_emits_candidate_for_cross_account_alternate_name(tmp_path):
    db = Database(tmp_path / "reg.db")
    registry = AccountRegistry(db)
    registry.upsert(Account(domain="avalara.com", name="Avalara Inc"))
    other = registry.upsert(Account(domain="track1099.com", name="Track1099"))
    registry.add_alias("Track1099", "name", other.domain, source="test")

    src = BbbProfileSource()
    cands = src.parse(_doc(), _account(), {"registry": registry, "today": "2026-08-31"})

    matches = [c for c in cands if c.evidence_data.get("alternate_name") == "Track1099"]
    assert len(matches) == 1
    c = matches[0]
    assert c.signal_type == "intent_3rd_topic"
    assert c.natural_key == f"bbba:avalara-inc:track1099"
    assert c.evidence_data["resolved_domain"] == "track1099.com"
    assert c.evidence_data["facts"]["business_name"] == "Avalara Inc"
    assert c.domain_override == "avalara.com"
    assert c.observed_at == "2026-08-31"


def test_parse_skips_alternate_names_resolving_to_same_account(tmp_path):
    db = Database(tmp_path / "reg.db")
    registry = AccountRegistry(db)
    registry.upsert(Account(domain="avalara.com", name="Avalara Inc"))
    # "Avalara" alternate name normalizes to the same account's own alias.
    registry.add_alias("Avalara", "name", "avalara.com", source="test")

    cands = BbbProfileSource().parse(
        _doc(), _account(), {"registry": registry, "today": "2026-08-31"}
    )

    assert all(
        c.evidence_data.get("resolved_domain") != "avalara.com" for c in cands
    )
    assert all(
        c.evidence_data.get("alternate_name") != "Avalara" for c in cands
    )


def test_parse_returns_empty_without_registry():
    assert BbbProfileSource().parse(_doc(), _account(), {}) == []
    assert BbbProfileSource().parse(_doc(), _account(), {"registry": None}) == []
