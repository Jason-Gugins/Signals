"""End-to-end (offline): a first-party article body becomes a promoted need.

Self-contained copy of tests/test_needs_harvest.py's harness (no cross-test
module import -- that would depend on sys.path). The whole chain is composed
with no fetcher: RawStore holds the article bytes, fetch_log holds the
company_feed provenance, and NeedsSource.local_harvest runs the real
render -> extract -> match -> promote path.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from src.core.db import Database
from src.core.models import Account
from src.core.rawstore import RawStore
from src.intel.market import load_market_profile
from src.sources.needs.collector import NeedsSource

TODAY = date(2026, 9, 14)
DOMAIN = "acme.com"
ARTICLE_URL = "https://acme.com/blog/merging-our-data-stack"

#: The seller-market phrase the article must match, verbatim.
MATCHING_PHRASE = "consolidating data across teams"

PROFILE_YAML = f"""\
profiles:
  test_needs_e2e:
    seller: Test Seller
    offerings:
      - id: offering_data_platform
        buyer_departments: [data]
        served_problem_phrases: [{MATCHING_PHRASE}]
        required_vendors: [snowflake]
        relevant_signal_types: [need_statement, required_stack_demand]
"""


def _vocab(*names: str) -> list[tuple[str, re.Pattern]]:
    return [(n, re.compile(rf"\b{re.escape(n)}\b", re.I)) for n in names]


VENDOR_VOCAB = _vocab("snowflake", "dbt")


def _profile(tmp_path):
    path = tmp_path / "markets.yaml"
    path.write_text(PROFILE_YAML, encoding="utf-8")
    return load_market_profile("test_needs_e2e", Path(path))


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


def _needs(out):
    return [c for c in out if c.signal_type == "need_statement"]


def test_article_body_becomes_a_promoted_need(tmp_path):
    db, raw, account, meta = _harness(tmp_path)
    body = (
        b"<article><h1>Post</h1>"
        b"<p>We are consolidating data across teams.</p></article>"
    )
    raw.put(
        source="company_feed",
        url=ARTICLE_URL,
        domain=DOMAIN,
        body=body,
        content_type="text/html",
        status=200,
    )
    db.execute(
        "INSERT INTO fetch_log(run_id, source, domain, url, status, bytes, at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("t1", "company_feed", DOMAIN, ARTICLE_URL, 200, len(body),
         "2026-09-15T01:12:51+00:00"),
    )

    first = _harvest(db, account, meta)

    needs = _needs(first)
    assert len(needs) == 1
    cand = needs[0]
    ev = cand.evidence_data
    # The article URL is the provenance, never a feed URL.
    assert ev["url"] == ARTICLE_URL
    quote = ev["quote"]
    assert "<" not in quote, f"raw markup leaked into the quote: {quote!r}"
    # html_to_text renders <h1>Post</h1> to its own line and the sentence
    # splitter only breaks on '. ' -- the heading therefore joins the sentence
    # into one piece. That is the real, current behaviour.
    assert quote == "Post\nWe are consolidating data across teams."
    assert "We are consolidating data across teams." in quote
    assert ev["matched_phrase"] == "we are consolidating"
    assert ev["source"] == "company_feed"
    assert ev["offering_id"] == "offering_data_platform"
    assert f"phrase:{MATCHING_PHRASE}" in ev["reasons"]

    second = _harvest(db, account, meta)

    assert len(second) == 1, "re-harvest must not duplicate the promotion"
    assert [c.natural_key for c in first] == [c.natural_key for c in second]
    assert cand.natural_key.startswith(f"need:{DOMAIN}:")


def test_unrelated_body_is_not_promoted(tmp_path):
    db, raw, account, meta = _harness(tmp_path)
    body = (
        b"<article><h1>Post</h1>"
        b"<p>We are consolidating data across teams.</p></article>"
    )
    # Same bytes, same 200 fetch, but a DIFFERENT source: no company_feed
    # label and no company_feed fetch_log row, so it is not first-party.
    raw.put(
        source="company_news",
        url=ARTICLE_URL,
        domain=DOMAIN,
        body=body,
        content_type="text/html",
        status=200,
    )
    db.execute(
        "INSERT INTO fetch_log(run_id, source, domain, url, status, bytes, at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("t2", "company_news", DOMAIN, ARTICLE_URL, 200, len(body),
         "2026-09-15T01:12:51+00:00"),
    )

    assert _needs(_harvest(db, account, meta)) == []


def test_feed_markup_never_reaches_the_extractor(tmp_path):
    db, raw, account, meta = _harness(tmp_path)
    script_only = b"<script>We are consolidating data across teams.</script>"
    script_plus_copy = (
        b"<script>We are consolidating data across teams.</script>"
        b"<p>Unrelated copy.</p>"
    )
    raw.put(
        source="company_feed",
        url="https://acme.com/blog/script-only",
        domain=DOMAIN,
        body=script_only,
        content_type="text/html",
        status=200,
    )
    raw.put(
        source="company_feed",
        url="https://acme.com/blog/script-plus-copy",
        domain=DOMAIN,
        body=script_plus_copy,
        content_type="text/html",
        status=200,
    )

    assert _needs(_harvest(db, account, meta)) == []


# --------------------------------------------------------------------------
# F10: the SAME article stored twice (dynamic page bytes -> two documents
# rows) is one sentence, therefore one signal -- the live darktrace.com defect
# where one boilerplate block became 15 identical need_statement signals.
# --------------------------------------------------------------------------

BOILERPLATE = "We are consolidating data across teams."


def test_one_article_stored_twice_promotes_one_need(tmp_path):
    db, raw, account, meta = _harness(tmp_path)
    first_body = b"<article><p>We are consolidating data across teams.</p></article>"
    second_body = (
        b"<article><p>We are consolidating data across teams.</p>"
        b"<!-- dynamic byte --></article>"
    )
    first = raw.put(
        source="company_feed", url=ARTICLE_URL, domain=DOMAIN, body=first_body,
        content_type="text/html", status=200,
        fetched_at="2026-09-10T01:00:00+00:00",
    )
    second = raw.put(
        source="company_feed", url=ARTICLE_URL, domain=DOMAIN, body=second_body,
        content_type="text/html", status=200,
        fetched_at="2026-09-15T01:00:00+00:00",
    )
    assert first.doc_id != second.doc_id, "the two copies must be separate rows"
    for at in ("2026-09-10T01:00:00+00:00", "2026-09-15T01:00:00+00:00"):
        db.execute(
            "INSERT INTO fetch_log(run_id, source, domain, url, status, bytes, at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("t3", "company_feed", DOMAIN, ARTICLE_URL, 200, len(first_body), at),
        )

    out = _harvest(db, account, meta)

    needs = _needs(out)
    assert len(needs) == 1, [c.evidence_data.get("url") for c in needs]
    ev = needs[0].evidence_data
    # The newest copy is the cited provenance; the sentence is the claim.
    assert ev["doc_id"] == second.doc_id
    assert ev["url"] == ARTICLE_URL
    assert BOILERPLATE in ev["quote"]
    assert second.doc_id not in needs[0].natural_key
