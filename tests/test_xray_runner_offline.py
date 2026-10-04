# tests/test_xray_runner_offline.py
"""X-ray runner end-to-end, fully OFFLINE: fetch/clock/sleep are injected fakes,
the Database and JSONL ledger are real (tmp_path). Per Task 7 as re-scoped by
the dispatch: ddg_lite-only attempt loop (body-validated, never status-validated),
never-guess persistence (identity_candidates keyed by the query, contacts only on
exact cohort match, unmatched profiles ledger-only), one ledger row per query.
Extended by the Google bypass ladder Task 6: run_xray(engine=...) threads the
engine through encode_query/parse/ledger; engine="google_state" parses W_jd
"2003" records and is validated against SUPPORTED_ENGINES.

Company-path bodies are small synthetic organic lite bodies (same markup shape as
tests/test_xray_serp_parse.py SYNTHETIC) because the real fixture
(tests/fixtures/xray/ddg_lite_serp.html) yields only LinkedIn profile URLs;
google_state bodies are synthetic "2003" record shapes, plus one run over the
real google_state fixture (LinkedIn profile records -> people path).
"""
import json
from pathlib import Path
from urllib.parse import quote, quote_plus

import pytest

from src.core.db import Database, IdentityCandidateStore
from src.core.textutil import guess_seniority, stable_id
from src.sources.xray.ledger import load_events
from src.sources.xray.runner import run_xray

FIX = Path(__file__).parent / "fixtures" / "xray"

CHALLENGE_BODY = (FIX / "ddg_challenge_202.html").read_text(encoding="utf-8")
CHALLENGE_MARKER = "unfortunately, bots use duckduckgo"  # first marker in that body
EMPTY_BODY = "<html><body>x</body></html>"  # clean, zero organic anchors

PACE = 0.25
PAUSE = 45.0
CLOCK_STAMP = "2026-10-02T00:00:00+00:00"


def _lite_body(*hits: tuple[str, str, str]) -> str:
    """Synthetic organic ddg-lite body: (url, title, snippet) per result."""
    rows = []
    for i, (url, title, snippet) in enumerate(hits, 1):
        wrapped = "//duckduckgo.com/l/?uddg=" + quote(url, safe="") + "&amp;rut=cafe"
        rows.append(
            f'  <tr><td>{i}.&nbsp;</td>'
            f"<td><a rel=\"nofollow\" href=\"{wrapped}\" class='result-link'>"
            f"{title}</a></td></tr>"
        )
        rows.append(
            f'  <tr><td></td><td class="result-snippet">{snippet}</td></tr>'
        )
    return (
        '<html><body>\n<table border="0">\n' + "\n".join(rows) + "\n</table></body></html>"
    )


ORG_COMPANY = _lite_body(
    ("https://newco.com/about", "Newco - About Us", "Newco raised a seed round last month."),
    ("https://blog.otherco.io/series-a", "OtherCo raised a Series A", "OtherCo announces its Series A."),
)
ORG_KNOWN_ROOT = _lite_body(
    ("https://www.acme.com/pricing", "Acme Corp pricing", "Acme is in the cohort already."),
)
ORG_PEOPLE = _lite_body(
    (
        "https://www.linkedin.com/in/janedoe",
        "Jane Doe - Head of Growth at Acme Corp | LinkedIn",
        "Jane runs growth at Acme.",
    ),
    (
        "https://www.linkedin.com/in/bobsmith",
        "Bob Smith - VP Sales at Nomatch Inc | LinkedIn",
        "Bob sells software.",
    ),
)

# Ready string specs (the runner receives load_library output; it never loads).
SPEC_ROLE = {"id": "comp_role", "kind": "company", "phrases": ["recently funded", "{role}"]}
SPEC_A = {"id": "comp_funded", "kind": "company", "phrases": ["recently funded"]}
SPEC_B = {"id": "comp_hiring", "kind": "company", "phrases": ["we are hiring"]}
SPEC_C = {"id": "comp_third", "kind": "company", "phrases": ["series a"]}
SPEC_PEOPLE = {
    "id": "people_head_growth",
    "kind": "people",
    "site": "linkedin.com/in",
    "phrases": ["head of growth"],
}
Q_ROLE = '"recently funded" "head of sales"'
Q_A = '"recently funded"'

ACME = {"domain": "acme.com", "name": "Acme Corp"}


def _fake_fetch(responses: list, urls: list):
    """fetch(url) -> (status, body) fake; an empty seq or an Exception item misbehaves loudly."""
    seq = list(responses)

    def fetch(url):
        urls.append(url)
        if not seq:
            raise AssertionError(f"unexpected fetch #{len(urls)}: {url}")
        item = seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return fetch


def _run(
    tmp_path,
    strings,
    responses,
    *,
    slots=None,
    kinds=None,
    accounts=(),
    pace_s=PACE,
    attempt_pause_s=PAUSE,
    max_attempts=2,
    max_queries=None,
    engine="ddg_lite",
):
    db = Database(tmp_path / "s.db")
    for acct in accounts:
        db.upsert("accounts", acct, pk="domain")
    ledger_path = tmp_path / "ledger.jsonl"
    urls: list = []
    sleeps: list = []
    report = run_xray(
        strings=strings,
        fetch=_fake_fetch(responses, urls),
        db=db,
        ledger_path=ledger_path,
        clock=lambda: CLOCK_STAMP,
        sleep=sleeps.append,
        slots=slots,
        kinds=kinds,
        pace_s=pace_s,
        attempt_pause_s=attempt_pause_s,
        max_attempts=max_attempts,
        max_queries=max_queries,
        engine=engine,
    )
    return report, db, load_events(ledger_path), urls, sleeps


# --- 1. company string queues identity_candidates (+ slots fill) ----------

def test_company_string_queues_identity_candidates(tmp_path):
    report, db, events, urls, _sleeps = _run(
        tmp_path, [SPEC_ROLE], [(200, ORG_COMPANY)], slots={"role": "head of sales"}
    )

    # ddg_lite encoding with the slot filled into the query
    assert len(urls) == 1
    assert urls[0].startswith("https://lite.duckduckgo.com/lite/?q=")
    assert "head+of+sales" in urls[0]

    rows = IdentityCandidateStore(db).pending_candidates("domain")
    assert len(rows) == 1
    row = rows[0]
    # The query string IS the review-row key: an operator query is not a
    # company name, so it is stored verbatim (never normalize_entity-mangled).
    assert row["name"] == Q_ROLE
    assert row["kind"] == "domain"
    assert row["source"] == "xray"
    assert row["status"] == "pending"
    candidates = json.loads(row["candidates_json"])
    assert candidates == [
        {
            "domain": "newco.com",
            "url": "https://newco.com/about",
            "title": "Newco - About Us",
            "snippet": "Newco raised a seed round last month.",
            "string_id": "comp_role",
        },
        {
            "domain": "blog.otherco.io",  # the hit's own host; dedupe ran on the root
            "url": "https://blog.otherco.io/series-a",
            "title": "OtherCo raised a Series A",
            "snippet": "OtherCo announces its Series A.",
            "string_id": "comp_role",
        },
    ]

    assert len(events) == 1
    ev = events[0]
    assert ev["string_id"] == "comp_role"
    assert ev["query"] == Q_ROLE
    assert ev["engine"] == "ddg_lite"
    assert ev["status"] == "ok"
    assert ev["attempts"] == 1
    assert ev["results"] == 2
    assert ev["profile_hits"] == 0
    assert ev["company_hits"] == 2
    assert ev["candidates_queued"] == 1
    assert ev["contacts_written"] == 0

    assert report["queries"] == 1
    assert report["ok"] == 1
    assert report["company_candidates"] == 1
    assert report["contacts_written"] == 0
    assert report["errors"] == []


# --- 2. known cohort root: no candidate row, ledger row still ok ----------

def test_known_cohort_root_produces_no_row_but_ledger_row(tmp_path):
    report, db, events, _urls, _sleeps = _run(
        tmp_path, [SPEC_A], [(200, ORG_KNOWN_ROOT)], accounts=[ACME]
    )

    assert db.query("SELECT * FROM identity_candidates") == []

    assert len(events) == 1
    ev = events[0]
    assert ev["status"] == "ok"
    assert ev["company_hits"] > 0  # the company URL was seen (evidence count)
    assert ev["candidates_queued"] == 0  # ...but its root is already an account

    assert report["ok"] == 1
    assert report["company_candidates"] == 0


# --- 3. profile hit matching a cohort account writes a contact ------------

def test_profile_hit_matching_cohort_account_writes_contact(tmp_path):
    report, db, events, _urls, _sleeps = _run(
        tmp_path, [SPEC_PEOPLE], [(200, ORG_PEOPLE)], accounts=[ACME]
    )

    contacts = db.query("SELECT * FROM contacts")
    assert len(contacts) == 1  # only the matched profile
    row = contacts[0]
    assert row["person_key"] == stable_id("janedoe", "acme.com")
    assert row["domain"] == "acme.com"
    assert row["name"] == ""  # SERP text is not a verified identity
    assert row["title"] == "Jane Doe - Head of Growth"
    assert row["seniority"] == guess_seniority("Jane Doe - Head of Growth") == "head"
    assert row["linkedin_slug"] == "janedoe"
    assert row["linkedin_url"] == "https://www.linkedin.com/in/janedoe"
    # bobsmith (Nomatch Inc) must have NO contact row and NO candidate row
    assert db.query("SELECT * FROM identity_candidates") == []

    assert len(events) == 1
    ev = events[0]
    assert ev["status"] == "ok"
    assert ev["profile_hits"] == 2
    assert ev["contacts_written"] == 1
    assert ev["profiles_unmatched"] > 0

    assert report["contacts_written"] == 1
    assert report["profiles_unmatched"] == 1


# --- 4. challenge exhausts attempts: recorded, nothing persisted ----------

def test_challenge_exhausts_attempts_records_and_persists_nothing(tmp_path):
    report, db, events, urls, sleeps = _run(
        tmp_path, [SPEC_A], [(202, CHALLENGE_BODY)] * 2
    )

    assert len(urls) == 2  # body-validated: both fetches happened despite 202s
    assert len(events) == 1
    ev = events[0]
    assert ev["status"] == "challenge"
    assert ev["attempts"] == 2
    assert ev["marker"] == CHALLENGE_MARKER
    assert ev["results"] == 0
    # pace before EVERY fetch, attempt_pause_s between attempts: [pace, pause, pace]
    assert sleeps == [PACE, PAUSE, PACE]

    assert db.query("SELECT * FROM identity_candidates") == []
    assert db.query("SELECT * FROM contacts") == []
    assert report["queries"] == 1
    assert report["challenge"] == 1
    assert report["ok"] == 0
    assert report["company_candidates"] == 0
    assert report["contacts_written"] == 0


# --- 5. challenge then organic: second attempt succeeds -------------------

def test_second_attempt_success_after_challenge(tmp_path):
    report, db, events, urls, sleeps = _run(
        tmp_path, [SPEC_A], [(202, CHALLENGE_BODY), (200, ORG_COMPANY)]
    )

    assert len(urls) == 2
    assert len(events) == 1
    ev = events[0]
    assert ev["status"] == "ok"
    assert ev["attempts"] == 2
    assert sleeps == [PACE, PAUSE, PACE]

    assert len(db.query("SELECT * FROM identity_candidates")) == 1
    assert report["ok"] == 1
    assert report["challenge"] == 0
    assert report["company_candidates"] == 1


# --- 6. pace_s sleep before every fetch -----------------------------------

def test_pace_sleep_before_every_fetch(tmp_path):
    responses = [(200, ORG_COMPANY), (200, ORG_COMPANY)]
    report, _db, events, urls, sleeps = _run(
        tmp_path, [SPEC_A, SPEC_B], responses, pace_s=PACE
    )

    assert len(urls) == 2
    assert len(events) == 2
    assert all(ev["status"] == "ok" for ev in events)
    assert sleeps == [PACE, PACE]  # exactly one pace sleep per fetch, nothing else


# --- 7. max_queries caps total fetches -------------------------------------

def test_max_queries_caps_fetches(tmp_path):
    responses = [(200, ORG_COMPANY), (200, ORG_COMPANY)]  # a 3rd fetch would raise
    report, _db, events, urls, _sleeps = _run(
        tmp_path, [SPEC_A, SPEC_B, SPEC_C], responses, max_queries=2
    )

    assert len(urls) == 2
    assert report["queries"] == 2
    assert report["ok"] == 2
    assert len(events) == 2


# --- 8. fetch exception: error row, run continues ---------------------------

def test_fetch_exception_records_error_and_continues(tmp_path):
    report, _db, events, urls, _sleeps = _run(
        tmp_path, [SPEC_A, SPEC_B], [RuntimeError("boom"), (200, ORG_COMPANY)]
    )

    assert len(urls) == 2  # no retry on transport errors: one fetch per query
    assert len(events) == 2
    first, second = events
    assert first["query"] == Q_A
    assert first["status"] == "error"
    assert first["attempts"] == 1
    assert first["error"] == "boom"
    assert second["status"] == "ok"

    assert report["queries"] == 2
    assert report["error"] == 1
    assert report["ok"] == 1
    assert report["errors"] == [{"query": Q_A, "reason": "boom"}]


# --- 9. clean zero-anchor body after attempts: status "empty" --------------

def test_empty_clean_body_after_attempts_records_empty(tmp_path):
    report, db, events, urls, sleeps = _run(
        tmp_path, [SPEC_A], [(200, EMPTY_BODY), (200, EMPTY_BODY)]
    )

    assert len(urls) == 2  # retried like a challenge, then recorded
    assert len(events) == 1
    ev = events[0]
    assert ev["status"] == "empty"
    assert ev["attempts"] == 2
    assert ev["results"] == 0
    assert sleeps == [PACE, PAUSE, PACE]

    assert db.query("SELECT * FROM identity_candidates") == []
    assert db.query("SELECT * FROM contacts") == []
    assert report["empty"] == 1
    assert report["ok"] == 0
    assert report["company_candidates"] == 0
    assert report["contacts_written"] == 0


# --- 10. empty built query: skip-and-ledger, never fetched ------------------

def test_empty_query_string_skips_and_ledgers(tmp_path):
    # A company spec whose only phrase is an unfilled slot builds "" — the
    # skip-and-ledger path (status "empty", attempts 0, no fetch at all).
    spec = {"id": "comp_slotless", "kind": "company", "phrases": ["{niche}"]}
    report, db, events, urls, _sleeps = _run(tmp_path, [spec], [])

    assert urls == []  # never fetched
    assert len(events) == 1
    ev = events[0]
    assert ev["string_id"] == "comp_slotless"
    assert ev["query"] == ""
    assert ev["status"] == "empty"
    assert ev["attempts"] == 0
    assert db.query("SELECT * FROM identity_candidates") == []
    assert report["queries"] == 0
    assert report["empty"] == 1


# --- 11. persistence failure: ledger row still written, run continues -------

def test_persistence_failure_still_ledgers(tmp_path):
    # The one-ledger-row-per-query contract holds even when persistence blows
    # up (locked sqlite etc.): the failure rides the event + report.
    class LockedDb:
        def query(self, sql, *a, **k):
            return []  # empty cohort

        def upsert(self, table, row, **k):
            raise RuntimeError("database is locked")

    ledger_path = tmp_path / "ledger.jsonl"
    urls: list = []
    report = run_xray(
        strings=[SPEC_A],
        fetch=_fake_fetch([(200, ORG_COMPANY)], urls),
        db=LockedDb(),
        ledger_path=ledger_path,
        clock=lambda: CLOCK_STAMP,
        sleep=lambda s: None,
    )
    events = load_events(ledger_path)

    assert len(events) == 1  # the ledger row was NOT lost
    ev = events[0]
    assert ev["status"] == "ok"
    assert ev["company_hits"] > 0
    assert "database is locked" in ev["persist_error"]
    assert report["errors"] == [{"query": Q_A, "reason": "persistence: database is locked"}]
    assert report["company_candidates"] == 0


# --- 12. google_state engine (bypass ladder Task 6) -------------------------

def _google_state_body(*hits: tuple[str, str]) -> str:
    """Synthetic organic google_state body: one "2003" record per (url, title).

    Same window.W_jd assignment shape as the real capture (and the fixture):
    [null,"<tok>","<url>","<title>",...] records inside the merged state blob.
    """
    recs = ", ".join(
        f'"k{i}":{{"2003":[null,"tok{i}","{url}","{title}",null,0]}}'
        for i, (url, title) in enumerate(hits, 1)
    )
    return (
        "<html><head><title>query - Google Search</title></head><body>"
        "<script>(function(){var m={" + recs + "};var a=m;"
        "if(window.W_jd)for(var b in a)window.W_jd[b]=a[b];"
        "else window.W_jd=a;})();</script></body></html>"
    )


GS_COMPANY = _google_state_body(
    ("https://newco.com/about", "Newco - About Us"),
    ("https://blog.otherco.io/series-a", "OtherCo raised a Series A"),
)


def test_google_state_engine_company_path(tmp_path):
    report, db, events, urls, _sleeps = _run(
        tmp_path, [SPEC_A], [(200, GS_COMPANY)], engine="google_state"
    )

    # google_state URL shape from encode_query (no num/filter params)
    assert len(urls) == 1
    assert urls[0] == f"https://www.google.com/search?q={quote_plus(Q_A)}&hl=en"

    rows = IdentityCandidateStore(db).pending_candidates("domain")
    assert len(rows) == 1
    candidates = json.loads(rows[0]["candidates_json"])
    assert [c["domain"] for c in candidates] == ["newco.com", "blog.otherco.io"]
    assert candidates[0]["snippet"] == ""  # W_jd records carry no snippet
    assert candidates[0]["title"] == "Newco - About Us"

    assert len(events) == 1
    ev = events[0]
    assert ev["engine"] == "google_state"  # the ledger carries the engine
    assert ev["status"] == "ok"
    assert ev["results"] == 2
    assert ev["company_hits"] == 2

    assert report["ok"] == 1
    assert report["company_candidates"] == 1


def test_google_state_engine_real_fixture_people_path(tmp_path):
    # The real minimized capture fixture flows end-to-end: 12 LinkedIn
    # profile records -> profile hits (ledger-only, cohort-unmatched).
    body = (FIX / "google_state_serp.html").read_text(encoding="utf-8")
    report, db, events, urls, _sleeps = _run(
        tmp_path, [SPEC_PEOPLE], [(200, body)], engine="google_state", accounts=[ACME]
    )

    assert urls == [
        "https://www.google.com/search?q="
        + quote_plus('site:linkedin.com/in "head of growth"')
        + "&hl=en"
    ]
    contacts = db.query("SELECT * FROM contacts")  # no cohort match: ledger-only
    assert contacts == []

    assert len(events) == 1
    ev = events[0]
    assert ev["engine"] == "google_state"
    assert ev["status"] == "ok"
    assert ev["results"] == 12
    assert ev["profile_hits"] == 12
    assert ev["profiles_unmatched"] == 12
    assert report["ok"] == 1


def test_runner_rejects_unknown_engine(tmp_path):
    # bing/brave/mojeek probed NO-GO (data/probe/XRAY_SERP_2026_10.md) —
    # validated up front, before any fetch.
    with pytest.raises(ValueError):
        _run(tmp_path, [SPEC_A], [(200, GS_COMPANY)], engine="bing")
