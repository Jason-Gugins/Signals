from datetime import date
from pathlib import Path
from src.sources.ats.workday import (
    parse_workday,
    parse_workday_detail,
    workday_body,
    workday_endpoint,
    workday_offsets,
)

_DETAIL_FIXTURE = Path(__file__).parent / "fixtures" / "ats" / "workday_job_detail.json"


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
