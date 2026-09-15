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
    assert len(details) == 10  # detail_follow_max default = 10
    assert all(t.meta["detail"] is True for t in details)
    assert all(t.headers["Accept"] == "application/json" for t in details)
    assert [t.meta["external_id"] for t in details] == [f"/job/R{i}" for i in range(10)]
    assert all(t.url == _LIST_BASE + t.meta["external_id"] for t in details)
    assert all(t.meta["base"] == _LIST_BASE and t.meta["token"] == "acme/wd5/acme" for t in details)
    assert [t.meta["title"] for t in details] == [f"Role {i}" for i in range(10)]


def test_workday_follow_tasks_cap_zero(monkeypatch):
    import src.sources.ats.collector as collector

    monkeypatch.setattr(collector, "load_ats_workday_cfg", lambda: {"detail_follow_max": 0})
    src = collector.WorkdaySource()
    follows = src.follow_tasks(_list_doc(), _acct(), _meta())
    assert [t for t in follows if t.method == "GET"] == []
    assert [t.json_body["offset"] for t in follows] == [o for o in workday_offsets(45, 20) if o != 0]


def test_workday_follow_tasks_cap_reads_config(monkeypatch):
    import src.sources.ats.collector as collector

    monkeypatch.setattr(collector, "load_ats_workday_cfg", lambda: {"detail_follow_max": 3})
    follows = collector.WorkdaySource().follow_tasks(_list_doc(), _acct(), _meta())
    assert [t.meta["external_id"] for t in follows if t.method == "GET"] == ["/job/R0", "/job/R1", "/job/R2"]


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
