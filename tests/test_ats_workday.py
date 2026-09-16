import json
from datetime import date
from pathlib import Path

from src.core.models import Account, Document
from src.sources.ats.collector import WorkdaySource
from src.sources.ats.workday import (
    parse_workday,
    parse_workday_detail,
    workday_body,
    workday_endpoint,
    workday_offsets,
)

_DETAIL_FIXTURE = Path(__file__).parent / "fixtures" / "ats" / "workday_job_detail.json"
_WORKDAY_ENDPOINT = workday_endpoint("acme", "wd5", "acme")


def test_workday_endpoint_and_body():
    assert workday_endpoint("acme", "wd5", "AcmeCareers").endswith("/wday/cxs/acme/AcmeCareers/jobs")
    assert workday_body(20, 20) == {"appliedFacets": {}, "limit": 20, "offset": 20, "searchText": ""}


def test_workday_offsets():
    assert workday_offsets(0) == []
    assert workday_offsets(20) == [0]
    assert workday_offsets(45, 20) == [0, 20, 40]
    assert workday_offsets(500, 20, cap=40) == [0, 20]


def test_parse_workday_relative_dates():
    today = date(2026, 8, 16)
    jobs = parse_workday(
        (Path(__file__).parent / "fixtures/ats/workday_jobs.json").read_bytes(),
        base="https://acme.wd5.myworkdayjobs.com/AcmeCareers",
        today=today,
    )
    assert len(jobs) == 2
    swe = next(j for j in jobs if "SWE" in j.external_id)
    assert swe.url.startswith("https://")
    assert swe.posted_at == "2026-08-13"
    rec = next(j for j in jobs if "REC" in j.external_id)
    assert rec.posted_at == "2026-07-17"
    assert rec.extra.get("posted_approx") is True
    assert rec.remote is True


def test_relative_forms_unit():
    from src.sources.ats.workday import _posted_on
    today = date(2026, 8, 16)
    assert _posted_on("Posted Today", today)[0] == "2026-08-16"
    assert _posted_on("Posted Yesterday", today)[0] == "2026-08-15"
    assert _posted_on("Posted 3 Days Ago", today)[0] == "2026-08-13"
    assert _posted_on("Posted 30+ Days Ago", today) == ("2026-07-17", True)


def test_parse_workday_detail_fixture():
    got = parse_workday_detail(_DETAIL_FIXTURE.read_bytes())
    assert isinstance(got["description"], str)
    assert got["description"]
    assert len(got["description"]) > 1000
    assert "<" not in got["description"]


def test_parse_workday_detail_fields():
    got = parse_workday_detail(_DETAIL_FIXTURE.read_bytes())
    assert got["employment_type"] == "Full time"
    assert got["start_date"] == "2026-09-09"
    assert got["external_url"].startswith("https://darktrace.wd3.myworkdayjobs.com/")


def test_parse_workday_detail_department_is_none():
    # The real CXS detail payload (fixture, captured live) has no
    # jobFamily / jobFamilyGroup / jobCategory key anywhere in jobPostingInfo,
    # so there is no source for department. It must always be None -- never a
    # guessed or inferred value.
    got = parse_workday_detail(_DETAIL_FIXTURE.read_bytes())
    assert got["department"] is None


def test_parse_workday_detail_key_set():
    got = parse_workday_detail(_DETAIL_FIXTURE.read_bytes())
    assert set(got) == {"description", "department", "employment_type", "start_date", "external_url"}


def test_parse_workday_detail_malformed():
    assert parse_workday_detail(b"") == {}
    assert parse_workday_detail(b"not json") == {}
    assert parse_workday_detail(b"[]") == {}


def test_parse_workday_detail_partial():
    got = parse_workday_detail(b'{"jobPostingInfo": {"title": "x"}}')
    assert got["description"] is None
    assert got["employment_type"] is None
    assert got["start_date"] is None
    assert got["external_url"] is None
    assert got["department"] is None


# ---------------------------------------------------------------------------
# C3: follow capped Workday detail URLs (descriptions live only behind the CXS
# job URL: base + externalPath).
# ---------------------------------------------------------------------------

_LIST_BASE = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/acme"


def _list_page(n: int = 12) -> bytes:
    """In-memory list page: `total` forces pagination, n postings feed details."""
    return json.dumps(
        {
            "total": 45,
            "jobPostings": [
                {
                    "title": f"Role {i}",
                    "externalPath": f"/job/R{i}",
                    "locationsText": "Austin, TX",
                    "postedOn": "Posted 3 Days Ago",
                }
                for i in range(n)
            ],
        }
    ).encode()


def _list_doc() -> Document:
    return Document(doc_id="w", source="ats_workday", url=_WORKDAY_ENDPOINT, body=_list_page())


def _acct() -> Account:
    return Account(domain="acme.com", ats_vendor="workday", ats_token="acme/wd5/acme")


def _meta() -> dict:
    return {
        "today": "2026-08-16",
        "base": _LIST_BASE,
        "token": "acme/wd5/acme",
    }


def test_workday_follow_tasks_emits_pagination_and_details():
    src = WorkdaySource()
    follows = src.follow_tasks(_list_doc(), _acct(), _meta())
    posts = [t for t in follows if t.method == "POST"]
    details = [t for t in follows if t.method == "GET"]
    assert [t.json_body["offset"] for t in posts] == [o for o in workday_offsets(45, 20) if o != 0]
    # F8: the ADAPTER applies no per-page cap any more -- every posting on the
    # page that still lacks a description gets a detail task, and the RUNNER
    # enforces the per-cycle budget (see detail_budget below and
    # tests/test_runner.py's rotation tests).
    assert len(details) == 12
    assert all(t.meta["detail"] is True for t in details)
    assert all(t.headers["Accept"] == "application/json" for t in details)
    assert [t.meta["external_id"] for t in details] == [f"/job/R{i}" for i in range(12)]
    assert all(t.url == _LIST_BASE + t.meta["external_id"] for t in details)
    assert all(t.meta["base"] == _LIST_BASE and t.meta["token"] == "acme/wd5/acme" for t in details)
    assert [t.meta["title"] for t in details] == [f"Role {i}" for i in range(12)]


def test_workday_follow_tasks_skips_described_postings():
    """Rotation: postings the runner says are ALREADY described are skipped.

    Same page, mixture of both, described ids in arbitrary positions -- the
    survivors keep page order and keep their own titles (no index shift).
    """
    meta = _meta()
    meta["described_external_ids"] = {"/job/R0", "/job/R3", "/job/R9"}
    follows = WorkdaySource().follow_tasks(_list_doc(), _acct(), meta)
    details = [t for t in follows if t.method == "GET"]
    skipped = (0, 3, 9)
    assert [t.meta["external_id"] for t in details] == [
        f"/job/R{i}" for i in range(12) if i not in skipped
    ]
    assert [t.meta["title"] for t in details] == [
        f"Role {i}" for i in range(12) if i not in skipped
    ]
    # pagination is untouched by the described-set filter
    posts = [t for t in follows if t.method == "POST"]
    assert [t.json_body["offset"] for t in posts] == [o for o in workday_offsets(45, 20) if o != 0]


def test_workday_follow_tasks_all_described_emits_no_details():
    meta = _meta()
    meta["described_external_ids"] = [f"/job/R{i}" for i in range(12)]
    follows = WorkdaySource().follow_tasks(_list_doc(), _acct(), meta)
    assert [t for t in follows if t.method == "GET"] == []
    assert [t.json_body["offset"] for t in follows if t.method == "POST"] == [o for o in workday_offsets(45, 20) if o != 0]


def test_workday_follow_tasks_ignores_described_ids_from_other_pages():
    """A described id that is not on THIS page changes nothing here."""
    meta = _meta()
    meta["described_external_ids"] = ["/job/OTHER-1", "/job/OTHER-2"]
    follows = WorkdaySource().follow_tasks(_list_doc(), _acct(), meta)
    assert len([t for t in follows if t.method == "GET"]) == 12


def test_workday_follow_tasks_no_per_page_cap():
    """The deliberate semantic change, asserted explicitly.

    30 postings on one page, none described -> 30 detail tasks. The old
    positional slice `postings[:detail_follow_max]` produced exactly 10, and
    that is what left 73 of 83 postings without a description forever.
    """
    doc = Document(doc_id="w", source="ats_workday", url=_WORKDAY_ENDPOINT, body=_list_page(30))
    details = [t for t in WorkdaySource().follow_tasks(doc, _acct(), _meta()) if t.method == "GET"]
    assert len(details) == 30
    assert [t.meta["external_id"] for t in details] == [f"/job/R{i}" for i in range(30)]
    assert all(t.headers["Accept"] == "application/json" for t in details)


def test_workday_follow_tasks_cap_zero(monkeypatch):
    """`detail_follow_max: 0` no longer silences the ADAPTER.

    The meaning moved from "per list page" to "per account per cycle", and the
    adapter no longer reads the number while building tasks at all: it emits
    every undescribed posting and reports the configured number to the runner
    (which is what drops them -- see test_runner.py's budget tests). Both
    halves of the contract are asserted here, so the update is a widening, not
    a weakening.
    """
    import src.sources.ats.collector as collector

    monkeypatch.setattr(collector, "load_ats_workday_cfg", lambda: {"detail_follow_max": 0})
    src = collector.WorkdaySource()
    follows = src.follow_tasks(_list_doc(), _acct(), _meta())
    # the adapter: uncapped, all 12 postings
    assert [t.meta["external_id"] for t in follows if t.method == "GET"] == [
        f"/job/R{i}" for i in range(12)
    ]
    assert [t.json_body["offset"] for t in follows if t.method == "POST"] == [o for o in workday_offsets(45, 20) if o != 0]
    # ... and the runner-facing opt-in it hands over
    assert src.detail_budget == 0
    assert src.detail_selection is True
    assert src.follow_passes == 3


def test_workday_follow_tasks_cap_reads_config(monkeypatch):
    """The loader is still the single source for the number, read at CALL time."""
    import src.sources.ats.collector as collector

    monkeypatch.setattr(collector, "load_ats_workday_cfg", lambda: {"detail_follow_max": 3})
    src = collector.WorkdaySource()
    follows = src.follow_tasks(_list_doc(), _acct(), _meta())
    # the adapter itself is uncapped ...
    assert [t.meta["external_id"] for t in follows if t.method == "GET"] == [
        f"/job/R{i}" for i in range(12)
    ]
    # ... it only REPORTS the configured per-cycle budget to the runner.
    assert src.detail_budget == 3


def test_workday_detail_doc_yields_no_follow_tasks():
    src = WorkdaySource()
    doc = Document(
        doc_id="d",
        source="ats_workday",
        url=_LIST_BASE + "/job/R0",
        body=_DETAIL_FIXTURE.read_bytes(),
    )
    assert src.follow_tasks(doc, _acct(), {"detail": True, "external_id": "/job/R0"}) == []


def test_workday_harvest_jobs_detail_branch():
    src = WorkdaySource()
    doc = Document(
        doc_id="d",
        source="ats_workday",
        url=_LIST_BASE + "/job/x",
        body=_DETAIL_FIXTURE.read_bytes(),
    )
    meta = {"detail": True, "external_id": "/job/x", "title": "Account Executive"}
    posts = src.harvest_jobs(doc, _acct(), meta)
    assert len(posts) == 1
    post = posts[0]
    assert post.description and len(post.description) > 1000
    assert post.posted_at is None
    assert post.title == "Account Executive"
    assert post.department is None
    assert post.external_id == "/job/x"
    assert post.url == _LIST_BASE + "/job/x"


def test_workday_harvest_jobs_detail_missing_title_is_none():
    src = WorkdaySource()
    doc = Document(
        doc_id="d",
        source="ats_workday",
        url=_LIST_BASE + "/job/x",
        body=_DETAIL_FIXTURE.read_bytes(),
    )
    posts = src.harvest_jobs(doc, _acct(), {"detail": True, "external_id": "/job/x"})
    assert len(posts) == 1
    assert posts[0].title is None  # never "" — COALESCE would blank the stored title
    assert posts[0].description


def test_workday_harvest_jobs_detail_malformed():
    src = WorkdaySource()
    doc = Document(doc_id="d", source="ats_workday", url=_LIST_BASE + "/job/x", body=b"not json")
    assert src.harvest_jobs(doc, _acct(), {"detail": True, "external_id": "/job/x"}) == []


def test_workday_detail_url_is_the_job_endpoint():
    src = WorkdaySource()
    follows = src.follow_tasks(_list_doc(), _acct(), _meta())
    first = next(t for t in follows if t.method == "GET")
    assert first.url == _LIST_BASE + "/job/R0"
    assert first.meta["external_id"] == "/job/R0"


def test_workday_follow_tasks_uses_real_list_fixture():
    src = WorkdaySource()
    doc = Document(
        doc_id="w",
        source="ats_workday",
        url=_WORKDAY_ENDPOINT,
        body=(Path(__file__).parent / "fixtures/ats/workday_jobs.json").read_bytes(),
    )
    follows = src.follow_tasks(doc, _acct(), _meta())
    details = [t for t in follows if t.method == "GET"]
    assert [t.url for t in details] == [_LIST_BASE + "/job/SWE-1", _LIST_BASE + "/job/REC-1"]
    assert [t.meta["title"] for t in details] == ["Software Engineer", "Recruiter"]
    assert [t for t in follows if t.method == "POST"] == []  # total 2 -> offsets [0] only


def test_workday_follow_tasks_no_body_or_bad_body():
    src = WorkdaySource()
    assert src.follow_tasks(Document(doc_id="e", source="ats_workday", body=None), _acct(), _meta()) == []
    bad = Document(doc_id="b", source="ats_workday", body=b"not json")
    assert src.follow_tasks(bad, _acct(), _meta()) == []


# ---------------------------------------------------------------------------
# C5: integration -- list pass + detail pass, through the REAL Runner, into
# storage, out to the demand extractor.
#
# C2 built the detail parser, C3 made the adapter follow the capped CXS detail
# URLs, C4 proved the runner's posting dedupe keeps the trend count honest.
# Still unproven was the CHAIN: one real Workday list page plus the real
# committed detail payload (darktrace.com job JR102301) driven through the
# actual Runner must leave ONE stored job row that keeps the list pass's own
# fields AND gains the detail pass's description.
# ---------------------------------------------------------------------------

_WORKDAY_TOKEN = "darktrace/wd3/DarktraceExternal"
# "tenant/wd/site", split exactly as WorkdaySource.plan() does:
# https://darktrace.wd3.myworkdayjobs.com/wday/cxs/darktrace/DarktraceExternal/jobs
_WORKDAY_LIST_URL = workday_endpoint("darktrace", "wd3", "DarktraceExternal")
# The CXS detail URL is the jobPostingSite base + the posting's externalPath.
_JOB_EXTERNAL_PATH = "/job/Amsterdam-Office-Netherlands/Account-Executive_JR102301"
_WORKDAY_DETAIL_URL = _WORKDAY_LIST_URL.rsplit("/jobs", 1)[0] + _JOB_EXTERNAL_PATH


def _real_list_body() -> bytes:
    """In-memory list page whose externalPath MATCHES the detail fixture's job.

    The committed tests/fixtures/ats/workday_jobs.json holds SWE-1/REC-1, not
    JR102301, and the two passes only share a `job_key` when the list posting's
    externalPath equals the detail fixture's path -- so the list body is built
    here instead of reusing that fixture. The REAL detail fixture carries no
    externalPath of its own (verified: no such key in its jobPostingInfo), so
    the posting's identity can only come from this list posting. total=1 keeps
    pagination out of the cycle.
    """
    return json.dumps(
        {
            "total": 1,
            "jobPostings": [
                {
                    "title": "Account Executive",
                    "externalPath": _JOB_EXTERNAL_PATH,
                    "locationsText": "Amsterdam, Netherlands",
                    "postedOn": "Posted 6 Days Ago",
                }
            ],
        }
    ).encode()


class _JsonFakeFetch:
    """FakeFetch pattern from tests/test_runner.py, with the JSON content-type.

    Copied rather than imported (test modules stay independent) and made
    strict: an unexpected URL fails loudly instead of being handed a b"ok"
    placeholder that could let the test pass for the wrong reason.
    """

    def __init__(self, by_url: dict):
        self.by_url = by_url
        self.calls: list[tuple[str, str]] = []

    def get(self, task, *, etag=None, last_modified=None):
        from src.core.http import FetchResult

        self.calls.append((task.method, task.url))
        if task.url not in self.by_url:
            raise AssertionError(f"unexpected fetch: {task.method} {task.url}")
        doc = Document(
            doc_id=f"d{len(self.calls)}",
            source=task.source,
            url=task.url,
            domain=task.domain,
            body=bytes(self.by_url[task.url]),
            status=200,
            content_type="application/json",
        )
        return FetchResult(True, 200, doc, False, None, 1)


def _run_cycle(tmp_path, accounts, adapters, by_url, **cfg_kw):
    """_harness() pattern from tests/test_runner.py (copied, not imported)."""
    from src.core.config import Config
    from src.core.db import Database
    from src.core.rawstore import RawStore
    from src.core.runlog import RunContext
    from src.identity.registry import AccountRegistry
    from src.pipeline.runner import CollectorRunner
    from src.signals.store import SignalStore
    from src.signals.taxonomy import Taxonomy

    db = Database(tmp_path / "s.db")
    cfg = Config()
    cfg.http.max_workers = int(cfg_kw.pop("max_workers", 1))
    cfg.http.respect_robots = False
    store = RawStore(db, tmp_path / "raw")
    tax = Taxonomy.load()
    ctx = RunContext(db, "collect")
    ctx.__enter__()
    runner = CollectorRunner(
        cfg,
        db,
        AccountRegistry(db),
        store,
        _JsonFakeFetch(by_url),
        SignalStore(db, tax),
        tax,
        ctx,
    )
    stats = runner.run(adapters, accounts, **cfg_kw)
    ctx.__exit__(None, None, None)
    return runner, stats, db


def test_runner_list_and_detail_reach_storage_and_the_demand_extractor(tmp_path, monkeypatch):
    """A real list page + the real detail payload, through the real Runner:

    ONE stored job row, carrying the detail pass's description AND the list
    pass's posted_at / location / title (the COALESCE merge), and whatever the
    production demand extractor genuinely makes of that stored description.
    """
    from datetime import datetime, timezone

    import src.sources.ats.collector as collector_module
    import src.sources.jobsignals.trend as trend_module
    from src.sources.ats.common import job_key
    from src.sources.ats.workday import _posted_on
    from src.sources.jobsignals.analyze import _vendor_vocab, extract_required_stack_demands

    acct = Account(
        domain="darktrace.com",
        name="Darktrace",
        ats_vendor="workday",
        ats_token=_WORKDAY_TOKEN,
    )
    by_url = {
        _WORKDAY_LIST_URL: _real_list_body(),
        _WORKDAY_DETAIL_URL: _DETAIL_FIXTURE.read_bytes(),
    }
    # The shared hiring-trend state file must never be written by a test:
    # capture the save instead (same pattern as test_runner.py's C4 test).
    saved: dict = {}
    monkeypatch.setattr(trend_module, "load_stats", lambda *a, **k: {})
    monkeypatch.setattr(trend_module, "save_stats", lambda stats, *a, **k: saved.update(stats))

    runner, stats, db = _run_cycle(
        tmp_path, [acct], [WorkdaySource()], by_url, max_passes=2, force=True
    )
    assert stats.failed == 0
    # Pass 0 planned the list POST url; pass 1 fetched the detail GET url. Both
    # really happened -- and nothing else did.
    assert set(runner.fetcher.calls) == {
        ("POST", _WORKDAY_LIST_URL),
        ("GET", _WORKDAY_DETAIL_URL),
    }

    key = job_key("ats_workday", _WORKDAY_TOKEN, _JOB_EXTERNAL_PATH)
    stored = db.one("SELECT * FROM jobs WHERE job_key = ?", (key,))
    assert stored is not None, f"no stored job row for {key}"
    row = dict(stored)

    # -- the detail pass landed -------------------------------------------
    assert isinstance(row["description"], str)
    assert row["description"]
    assert "<" not in row["description"]
    assert len(row["description"]) > 1000  # the real fixture renders ~4 KB

    # -- the list pass's own fields survived (COALESCE) --------------------
    # posted_at can only come from the list posting: WorkdaySource's detail
    # branch passes posted_at=None deliberately, and the detail payload carries
    # only human text / a boolean. (_posted_on mirrors the runner's own clock:
    # the runner injects meta["today"] = its UTC date.)
    expected_posted = _posted_on("Posted 6 Days Ago", datetime.now(timezone.utc).date())[0]
    assert row["posted_at"] == expected_posted
    assert row["location_raw"] == "Amsterdam, Netherlands"
    assert row["city"] == "Amsterdam"
    assert row["title"] == "Account Executive"
    # The real CXS detail payload has no department (jobFamily/jobCategory)
    # field at all, so asserting a value here would assert a fiction.
    assert row["department"] is None
    assert row["first_seen_at"]
    assert row["closed_at"] is None

    # -- the stored description reaches the production demand extractor ----
    # Run against the production vocabulary (_vendor_vocab(): the techstack
    # fingerprint vendor keys + the supplemental B2B set) and the ACTUAL stored
    # text. No allowlisted vendor term appears anywhere in the rendered fixture
    # (nor inside the one required-stack phrase it contains, "experience in a
    # high-growth business environment"), so the honest assertion is that the
    # call returns a list -- and what it returns is recorded, not invented.
    demands = extract_required_stack_demands(row["description"], _vendor_vocab())
    assert isinstance(demands, list)
    assert demands == []  # observed: no allowlisted vendor in the real text

    # -- characterization: upsert_jobs needed NO change for this ------------
    # src/core/db.py's upsert(..., coalesce=True) writes
    # COALESCE(excluded.col, jobs.col) for every column NOT named in
    # `overwrite`, and src/sources/ats/common.py's upsert_jobs passes
    # overwrite={"last_seen_at"} -- so the detail pass's description merged into
    # the list pass's row, and a later description-less pass can only refresh
    # last_seen_at: it cannot blank the stored text.
    monkeypatch.setattr(
        collector_module, "load_ats_workday_cfg", lambda: {"detail_follow_max": 0}
    )
    _, stats2, _ = _run_cycle(
        tmp_path,
        [acct],
        [WorkdaySource()],
        {_WORKDAY_LIST_URL: _real_list_body()},
        max_passes=2,
        force=True,
    )
    assert stats2.failed == 0
    row2 = dict(db.one("SELECT * FROM jobs WHERE job_key = ?", (key,)))
    assert row2["description"] == row["description"]
    assert row2["posted_at"] == row["posted_at"]
    assert row2["location_raw"] == row["location_raw"]
    assert row2["first_seen_at"] == row["first_seen_at"]
    assert row2["last_seen_at"] >= row["last_seen_at"]

    # -- C4's guarantee through the real adapter: count ONCE ---------------
    # The detail pass re-described a posting the list pass already harvested,
    # so the domain's hiring-trend count must stay at the LIST-ONLY count (1),
    # never 2 -- pre-C4 the detail pass doubled it and could fake a surge.
    assert saved.get("darktrace.com", {}).get("count") == 1
    snap = db.one("SELECT open_count FROM job_snapshots WHERE domain='darktrace.com'")
    assert snap is not None and snap["open_count"] == 1
