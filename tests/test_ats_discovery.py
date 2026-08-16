"""Tests for ATS vendor/token discovery from HTML fixtures."""

from __future__ import annotations

from pathlib import Path

from src.identity.ats_discovery import careers_url_candidates, detect_ats


FIXTURES = Path(__file__).parent / "fixtures" / "ats"
VENDORS = [
    "greenhouse",
    "lever",
    "ashby",
    "smartrecruiters",
    "workable",
    "recruitee",
    "workday",
    "bamboohr",
    "jazzhr",
    "personio",
    "teamtailor",
]


def test_each_vendor_detected_from_fixture():
    for vendor in VENDORS:
        html = (FIXTURES / f"careers_{vendor}.html").read_text(encoding="utf-8")
        matches = detect_ats(html, "https://acme.com/careers")
        assert matches, vendor
        assert matches[0].vendor == vendor
        assert matches[0].token
        assert matches[0].confidence > 0.5


def test_workday_extras():
    html = (FIXTURES / "careers_workday.html").read_text(encoding="utf-8")
    m = detect_ats(html, "https://acme.com/careers")[0]
    assert m.vendor == "workday"
    assert m.extra["tenant"] == "acme"
    assert m.extra["wd"] == "wd5"
    assert m.extra["site"] == "AcmeCareers"


def test_no_match_empty():
    assert detect_ats("<html><body>We hire people</body></html>", "https://acme.com") == []


def test_iframe_beats_comment():
    html = (FIXTURES / "careers_mixed.html").read_text(encoding="utf-8")
    matches = detect_ats(html, "https://nvidia.com/careers")
    assert [m.vendor for m in matches][0] == "workday"
    assert matches[0].confidence > next(m.confidence for m in matches if m.vendor == "greenhouse")


def test_careers_url_candidates_stable():
    got = careers_url_candidates("acme.com")
    assert got == [
        "https://acme.com/careers",
        "https://acme.com/jobs",
        "https://acme.com/company/careers",
        "https://careers.acme.com/",
        "https://jobs.acme.com/",
        "https://acme.com/about/careers",
        "https://acme.com/join-us",
    ]
    assert careers_url_candidates("acme.com") == got
