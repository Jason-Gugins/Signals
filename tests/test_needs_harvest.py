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
from src.core.textutil import sha256_hex
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


def _quote_key(quote: str) -> str:
    """The quote-scoped natural key: normalised quote text, no doc_id."""
    return f"need:{DOMAIN}:{sha256_hex(' '.join(quote.split()))[:12]}"


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
    # The quote is the claim and the document is only its provenance, so the
    # key is quote-scoped: a re-stored copy of the same sentence is the SAME
    # signal, not a new one.
    assert cand.natural_key == _quote_key(ev["quote"])
    assert doc.doc_id not in cand.natural_key
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


def test_html_paragraph_yields_verbatim_sentence(tmp_path):
    db, store, account, meta = _harness(tmp_path)
    store.put(
        source="company_feed",
        url="https://acme.com/blog/data-platform",
        domain=DOMAIN,
        body=b"<p>We are consolidating data across teams.</p>",
        content_type="text/html",
        status=200,
    )

    needs = [c for c in _harvest(db, account, meta) if c.signal_type == "need_statement"]

    assert len(needs) == 1
    quote = needs[0].evidence_data["quote"]
    assert "<" not in quote, f"raw markup leaked into the quote: {quote!r}"
    assert quote == "We are consolidating data across teams."


def test_sentence_only_inside_script_is_ignored(tmp_path):
    db, store, account, meta = _harness(tmp_path)
    store.put(
        source="company_feed",
        url="https://acme.com/blog/scripts",
        domain=DOMAIN,
        body=b"<script>We are consolidating data across teams.</script>"
        b"<p>Unrelated copy.</p>",
        content_type="text/html",
        status=200,
    )

    assert [
        c.signal_type for c in _harvest(db, account, meta)
    ].count("need_statement") == 0


def test_plain_text_body_is_unchanged_by_rendering(tmp_path):
    db, store, account, meta = _harness(tmp_path)
    store.put(
        source="company_feed",
        url="https://acme.com/blog/plain",
        domain=DOMAIN,
        body=b"We are consolidating data across teams.",
        content_type="text/html",
        status=200,
    )

    needs = [c for c in _harvest(db, account, meta) if c.signal_type == "need_statement"]

    assert len(needs) == 1
    assert needs[0].evidence_data["quote"] == "We are consolidating data across teams."


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


# --------------------------------------------------------------------------
# Part 1b: first-party resolution by fetch provenance, not documents.source
# --------------------------------------------------------------------------


def test_first_party_doc_is_found_even_when_another_source_stored_it(tmp_path):
    db, raw, account, meta = _harness(tmp_path)
    url = "https://acme.com/blog/rss.xml"
    body = b"We are consolidating data across teams."
    raw.put(source="feed_discovery", url=url, body=body,
            content_type="application/rss+xml", status=200, domain=DOMAIN)
    db.execute(
        "INSERT INTO fetch_log(run_id, source, domain, url, status, bytes, at) VALUES (?,?,?,?,?,?,?)",
        ("t1", "company_feed", DOMAIN, url, 200, len(body), "2026-09-15T01:12:51+00:00"),
    )
    out = _harvest(db, account, meta)
    assert [c.signal_type for c in out].count("need_statement") == 1


def test_provenance_does_not_leak_across_domains(tmp_path):
    db, raw, account, meta = _harness(tmp_path)
    url = "https://other.example/blog/rss.xml"
    body = b"We are consolidating data across teams."
    raw.put(source="feed_discovery", url=url, body=body,
            content_type="application/rss+xml", status=200, domain="other.example")
    db.execute(
        "INSERT INTO fetch_log(run_id, source, domain, url, status, bytes, at) VALUES (?,?,?,?,?,?,?)",
        ("t2", "company_feed", "other.example", url, 200, len(body), "2026-09-15T01:12:51+00:00"),
    )
    assert [c.signal_type for c in _harvest(db, account, meta)].count("need_statement") == 0


def test_non_2xx_provenance_is_not_first_party(tmp_path):
    db, raw, account, meta = _harness(tmp_path)
    url = "https://acme.com/blog/rss.xml"
    body = b"We are consolidating data across teams."
    raw.put(source="feed_discovery", url=url, body=body,
            content_type="application/rss+xml", status=200, domain=DOMAIN)
    db.execute(
        "INSERT INTO fetch_log(run_id, source, domain, url, status, bytes, at) VALUES (?,?,?,?,?,?,?)",
        ("t3", "company_feed", DOMAIN, url, 404, 0, "2026-09-15T01:12:51+00:00"),
    )
    assert [c.signal_type for c in _harvest(db, account, meta)].count("need_statement") == 0


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


# --------------------------------------------------------------------------
# Part 1c (F10): quote-level duplication is NOT signal volume.
#
# Content-addressed storage mints a new documents row whenever the body bytes
# differ at all, so one company sentence can be stored N times. The signal is
# the QUOTE; the document is only its provenance. At most one need_statement
# per distinct normalised quote text is promoted per harvest.
# --------------------------------------------------------------------------

QUOTE_SENTENCE = "We are consolidating data across teams."


def _quote_docs(store, *, url, body, fetched_at):
    return store.put(
        source="company_feed",
        url=url,
        domain=DOMAIN,
        body=body,
        content_type="text/html",
        status=200,
        fetched_at=fetched_at,
    )


def _needs(out):
    return [c for c in out if c.signal_type == "need_statement"]


def test_three_documents_with_the_same_sentence_promote_one_need(tmp_path):
    db, store, account, meta = _harness(tmp_path)
    # Same sentence, three different bodies (markup / whitespace only), so
    # content-addressed storage creates three distinct documents rows.
    _quote_docs(
        store, url="https://acme.com/blog/a",
        body=b"<p>We are consolidating data across teams.</p>",
        fetched_at="2026-09-10T10:00:00+00:00",
    )
    _quote_docs(
        store, url="https://acme.com/blog/b",
        body=b"<div>We are consolidating data across teams.</div>\n",
        fetched_at="2026-09-13T10:00:00+00:00",
    )
    _quote_docs(
        store, url="https://acme.com/blog/c",
        body=b"<p>We are consolidating data  across teams.</p>",
        fetched_at="2026-09-11T10:00:00+00:00",
    )

    needs = _needs(_harvest(db, account, meta))

    assert len(needs) == 1, [c.evidence_data.get("quote") for c in needs]


def test_newest_document_wins_and_its_provenance_is_cited(tmp_path):
    db, store, account, meta = _harness(tmp_path)
    _quote_docs(
        store, url="https://acme.com/blog/old",
        body=b"<p>We are consolidating data across teams.</p>",
        fetched_at="2026-09-10T10:00:00+00:00",
    )
    newest = _quote_docs(
        store, url="https://acme.com/blog/new",
        body=b"<div>We are consolidating data across teams.</div>",
        fetched_at="2026-09-13T10:00:00+00:00",
    )
    _quote_docs(
        store, url="https://acme.com/blog/mid",
        body=b"<p>We are consolidating data across teams.</p> ",
        fetched_at="2026-09-11T10:00:00+00:00",
    )

    needs = _needs(_harvest(db, account, meta))

    assert len(needs) == 1
    ev = needs[0].evidence_data
    assert ev["doc_id"] == newest.doc_id
    assert ev["url"] == "https://acme.com/blog/new"
    assert ev["fetched_at"] == "2026-09-13T10:00:00+00:00"
    assert ev["quote"] == QUOTE_SENTENCE


def test_same_fetched_at_ties_break_deterministically_by_doc_id(tmp_path):
    db, store, account, meta = _harness(tmp_path)
    same = "2026-09-12T10:00:00+00:00"
    first = _quote_docs(
        store, url="https://acme.com/blog/one",
        body=b"<p>We are consolidating data across teams.</p>", fetched_at=same,
    )
    second = _quote_docs(
        store, url="https://acme.com/blog/two",
        body=b"<div>We are consolidating data across teams.</div>", fetched_at=same,
    )

    needs = _needs(_harvest(db, account, meta))

    assert len(needs) == 1
    # Documented tie-break: the LEXICOGRAPHICALLY LARGEST doc_id wins, so the
    # winner does not depend on row order (stable across runs and machines).
    assert needs[0].evidence_data["doc_id"] == max(first.doc_id, second.doc_id)


def test_a_later_copy_of_the_same_sentence_does_not_add_a_signal(tmp_path):
    db, store, account, meta = _harness(tmp_path)
    _quote_docs(
        store, url="https://acme.com/blog/v1",
        body=b"<p>We are consolidating data across teams.</p>",
        fetched_at="2026-09-10T10:00:00+00:00",
    )

    first = _needs(_harvest(db, account, meta))
    assert len(first) == 1
    key = first[0].natural_key

    # The page's dynamic bytes change -> a NEW document row for the SAME
    # sentence (the exact live mechanism, 2026-09-16).
    later = _quote_docs(
        store, url="https://acme.com/blog/v2",
        body=b"<p>We are consolidating data across teams.</p><!-- r2 -->",
        fetched_at="2026-09-14T10:00:00+00:00",
    )

    second = _needs(_harvest(db, account, meta))
    assert len(second) == 1, [c.evidence_data.get("url") for c in second]
    assert second[0].natural_key == key, "the key must not depend on the copy"
    # Provenance still tracks the newest copy.
    assert second[0].evidence_data["doc_id"] == later.doc_id


def test_two_distinct_sentences_still_promote_two_needs(tmp_path):
    db, store, account, meta = _harness(tmp_path)
    _quote_docs(
        store, url="https://acme.com/blog/build",
        body=b"<p>We are building a new pipeline while consolidating data across teams.</p>",
        fetched_at="2026-09-10T10:00:00+00:00",
    )
    _quote_docs(
        store, url="https://acme.com/blog/consolidate",
        body=b"<p>We are consolidating data across teams.</p>",
        fetched_at="2026-09-11T10:00:00+00:00",
    )

    needs = _needs(_harvest(db, account, meta))

    assert len(needs) == 2, [c.evidence_data.get("quote") for c in needs]
    assert len({c.natural_key for c in needs}) == 2
