"""Tests for plan_funding (pure, no I/O)."""

from __future__ import annotations

from datetime import date

import pytest

from src.identity.edgar_ids import SUBMISSIONS_URL, pad_cik
from src.pipeline.funding import SOURCE, homepage_url, plan_company_queries, plan_funding
from src.sources.sec.fts import fts_search_url


TODAY = date(2026, 8, 22)


def test_plan_recent_efts_url():
    tasks = plan_funding("recent", today=TODAY, days=30, size=100, limit=100)
    assert len(tasks) == 1
    t = tasks[0]
    assert t.source == SOURCE == "sec_formd"
    assert t.domain is None
    want = fts_search_url(forms=("D",), start="2026-07-23", end="2026-08-22", offset=0, size=100)
    assert t.url == want
    assert t.meta["kind"] == "fts"
    assert t.meta["mode"] == "recent"
    assert t.meta["limit"] == 100
    assert t.meta["today"] == "2026-08-22"
    assert "q=" not in t.url


def test_plan_search_requires_q():
    with pytest.raises(ValueError):
        plan_funding("search", today=TODAY)
    tasks = plan_funding("search", today=TODAY, q="robotics", days=30)
    assert tasks[0].meta["kind"] == "fts"
    assert "q=robotics" in tasks[0].url
    assert "forms=D" in tasks[0].url


def test_plan_company_cik_uses_submissions():
    tasks = plan_funding("company", today=TODAY, cik="1234567")
    assert len(tasks) == 1
    assert tasks[0].url == SUBMISSIONS_URL.format(cik10=pad_cik("1234567"))
    assert tasks[0].meta["kind"] == "submissions"
    assert tasks[0].meta["cik"] == "0001234567"
    assert tasks[0].domain is None


def test_plan_company_name_no_date_range():
    tasks = plan_funding("company", today=TODAY, q="Acme")
    assert len(tasks) == 1
    assert tasks[0].meta["kind"] == "fts"
    url = tasks[0].url
    assert 'q=%22Acme%22' in url or 'q="Acme"' in url
    assert "startdt=" not in url
    assert "enddt=" not in url
    assert "forms=D" in url


def test_plan_company_needs_cik_or_q():
    with pytest.raises(ValueError):
        plan_funding("company", today=TODAY)


def test_plan_unknown_mode():
    with pytest.raises(ValueError):
        plan_funding("nope", today=TODAY)


def test_homepage_url_uses_root_domain():
    assert homepage_url("https://radicl.com/") == "https://radicl.com/"
    assert homepage_url("radicl.com") == "https://radicl.com/"
    assert homepage_url("https://www.scanner.dev/foo") == "https://scanner.dev/"


def test_plan_company_queries_quoted_no_dates():
    tasks = plan_company_queries(["Scanner, Inc", "Scanner"], today=TODAY, limit=8)
    assert len(tasks) == 2
    for t in tasks:
        assert t.meta["kind"] == "fts"
        assert "startdt=" not in t.url
        assert "enddt=" not in t.url
        assert "q=%22" in t.url
        assert "forms=D" in t.url


def test_plan_company_queries_caps_at_three():
    tasks = plan_company_queries(["A", "B", "C", "D"], today=TODAY, limit=5)
    assert len(tasks) == 3
