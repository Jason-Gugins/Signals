"""Task 8b: the local needs source.

Part 1 promotes first-party need statements (company_feed documents) that
match the selected seller-market profile. Part 2 promotes explicit
required-stack demands from open jobs. Both are evidence-cited: the verbatim
quote / job text is the proof, and absence is never claimed (the source does
not read the technologies table at all).
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from src.core.db import Database
from src.core.models import Account, Document
from src.core.rawstore import RawStore
from src.intel.market import load_market_profile
from src.sources.needs.collector import NeedsSource

TODAY = date(2026, 9, 14)
DOMAIN = "acme.com"
OFFERING_ID = "offering_data_platform"

PROFILE_YAML = """\
profiles:
  test_needs:
    seller: Test Seller
    offerings:
      - id: offering_data_platform
        buyer_departments: [data]
        served_problem_phrases: [consolidating data]
        required_vendors: [snowflake]
        relevant_signal_types: [need_statement, required_stack_demand]
"""


def _vocab(*names: str) -> list[tuple[str, re.Pattern]]:
    return [(n, re.compile(rf"\b{re.escape(n)}\b", re.I)) for n in names]


VENDOR_VOCAB = _vocab("snowflake", "dbt")


def _profile(tmp_path):
    path = tmp_path / "markets.yaml"
    path.write_text(PROFILE_YAML, encoding="utf-8")
    return load_market_profile("test_needs", Path(path))


def _harness(tmp_path):
    db = Database(tmp_path / "s.db")
    store = RawStore(db, tmp_path / "raw")
    account = Account(domain=DOMAIN, name="Acme")
    meta = {
        "raw_store": store,
        "market_profile": _profile(tmp_path),
        "vendor_vocab": VENDOR_VOCAB,
    }
    return db, store, account, meta


def _harvest(db, account, meta):
    return NeedsSource().local_harvest(
        db=db, account=account, today=TODAY, task_meta=meta
    )


def _insert_job(db, *, job_key, title="Data Engineer", department="data",
                url="https://jobs.acme.com/1", description="x",
                closed_at=None):
    db.execute(
        """INSERT INTO jobs (job_key, domain, source, title, department,
                             description, url, closed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (job_key, DOMAIN, "ats_greenhouse", title, department, description,
         url, closed_at),
    )


def _evidence_blob(candidate) -> str:
    """Every key and value in evidence_data, flattened to lowercase text."""
    parts: list[str] = []
    for key, value in candidate.evidence_data.items():
        parts.append(str(key))
        if isinstance(value, (list, tuple)):
            parts.extend(str(v) for v in value)
        else:
            parts.append(str(value))
    return " | ".join(parts).lower()


# --------------------------------------------------------------------------
# Part 1: need statements from first-party documents
# --------------------------------------------------------------------------


def test_matching_need_is_emitted_with_document_provenance(tmp_path):
    db, store, account, meta = _harness(tmp_path)
    body = b"We are consolidating data across teams."
    doc = store.put(
        source="company_feed",
        url="https://acme.com/blog/data-platform",
        domain=DOMAIN,
        body=body,
        content_type="text/html",
        status=200,
        fetched_at="2026-09-13T10:00:00+00:00",
    )

    out = _harvest(db, account, meta)

    needs = [c for c in out if c.signal_type == "need_statement"]
    assert len(needs) == 1
    cand = needs[0]
    ev = cand.evidence_data
    assert ev["quote"] == "We are consolidating data across teams."
    assert ev["matched_phrase"] == "we are consolidating"
    assert ev["doc_id"] == doc.doc_id
    assert ev["url"] == "https://acme.com/blog/data-platform"
    assert ev["source"] == "company_feed"
    assert ev["offering_id"] == OFFERING_ID
    assert ev.get("reasons")
    assert "phrase:consolidating data" in ev["reasons"]
    assert ev["fetched_at"] == "2026-09-13T10:00:00+00:00"
    assert cand.natural_key.startswith(
        f"need:{DOMAIN}:{doc.doc_id}:"
    )
    assert cand.observed_at == TODAY.isoformat()
    assert cand.confidence == 0.7


def test_unrelated_quote_is_not_promoted(tmp_path):
    db, store, account, meta = _harness(tmp_path)
    store.put(
        source="company_feed",
        url="https://acme.com/blog/office",
        domain=DOMAIN,
        body=b"We are redesigning the office kitchen.",
        content_type="text/html",
        status=200,
    )

    assert _harvest(db, account, meta) == []


def test_missing_body_is_skipped_without_raising(tmp_path):
    db, store, account, meta = _harness(tmp_path)
    ghost = Document(
        doc_id="a" * 64,
        source="company_feed",
        url="https://acme.com/blog/gone",
        domain=DOMAIN,
        status=200,
        byte_size=12,
        path=str(tmp_path / "raw" / "aa" / "does-not-exist.gz"),
        fetched_at="2026-09-13T10:00:00+00:00",
    )
    db.upsert("documents", ghost.to_db_row(), pk="doc_id")

    assert _harvest(db, account, meta) == []


# --------------------------------------------------------------------------
# Part 2: explicit required-stack demands from open jobs
# --------------------------------------------------------------------------


def test_matching_required_stack_demand_is_emitted_with_job_provenance(tmp_path):
    db, _store, account, meta = _harness(tmp_path)
    _insert_job(
        db,
        job_key="acme-data-eng",
        title="Senior Data Engineer",
        department="data",
        url="https://jobs.acme.com/data-eng",
        description="Required: hands-on experience with Snowflake.",
    )

    out = _harvest(db, account, meta)

    demands = [c for c in out if c.signal_type == "required_stack_demand"]
    assert len(demands) == 1
    cand = demands[0]
    ev = cand.evidence_data
    assert ev["job_key"] == "acme-data-eng"
    assert ev["job_url"] == "https://jobs.acme.com/data-eng"
    assert ev["vendor"] == "snowflake"
    assert ev["phrase"] == "Snowflake"
    assert ev["department"] == "data"
    assert ev["offering_id"] == OFFERING_ID
    assert ev.get("reasons")
    assert "vendor:snowflake" in ev["reasons"]
    assert ev["title"] == "Senior Data Engineer"
    assert cand.natural_key == f"stack-demand:{DOMAIN}:acme-data-eng:snowflake"
    assert cand.title == "Senior Data Engineer"
    assert cand.observed_at == TODAY.isoformat()
    assert cand.confidence == 0.7


def test_unrelated_required_vendor_is_not_promoted(tmp_path):
    db, _store, account, meta = _harness(tmp_path)
    _insert_job(
        db,
        job_key="acme-dbt",
        title="Analytics Engineer",
        department="data",
        description="Required: hands-on experience with dbt.",
    )

    assert _harvest(db, account, meta) == []


def test_demands_do_not_depend_on_observed_technologies_and_never_claim_absence(tmp_path):
    db, _store, account, meta = _harness(tmp_path)
    assert db.one(
        "SELECT COUNT(*) AS n FROM technologies WHERE domain=?", (DOMAIN,)
    )["n"] == 0

    _insert_job(
        db,
        job_key="acme-snowflake",
        title="Data Platform Engineer",
        department="data",
        description="Required: hands-on experience with Snowflake.",
    )

    out = _harvest(db, account, meta)
    demands = [c for c in out if c.signal_type == "required_stack_demand"]
    assert len(demands) == 1, "a zero-technology account must still emit the demand"

    for cand in out:
        blob = _evidence_blob(cand)
        for forbidden in ("absent", "missing", "not observed", "shift"):
            assert forbidden not in blob, f"evidence claims absence via {forbidden!r}"


def test_harvest_is_idempotent_by_natural_key(tmp_path):
    db, store, account, meta = _harness(tmp_path)
    store.put(
        source="company_feed",
        url="https://acme.com/blog/data-platform",
        domain=DOMAIN,
        body=b"We are consolidating data across teams.",
        content_type="text/html",
        status=200,
    )
    _insert_job(
        db,
        job_key="acme-data-eng",
        description="Required: hands-on experience with Snowflake.",
    )

    first = _harvest(db, account, meta)
    second = _harvest(db, account, meta)

    assert len(first) == len(second)
    assert [c.natural_key for c in first] == [c.natural_key for c in second]
    assert len(first) == 2
