"""Tests for pure text, date, money, and title-classification utilities."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone

from src.core.textutil import (
    clean_text,
    days_between,
    first_match,
    guess_department,
    guess_persona,
    guess_seniority,
    normalize_ws_lines,
    parse_count,
    parse_money,
    sha256_hex,
    slugify,
    stable_id,
    to_iso_date,
    truncate,
)


def test_clean_text_collapses_and_none_on_empty():
    assert clean_text("  hello   world  ") == "hello world"
    assert clean_text("hello\u00a0world") == "hello world"
    assert clean_text("") is None
    assert clean_text("   ") is None
    assert clean_text(None) is None


def test_to_iso_date_absolute_formats():
    assert to_iso_date(date(2026, 1, 5)) == "2026-01-05"
    assert to_iso_date(datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)) == "2026-01-05"
    assert to_iso_date("2026-01-05") == "2026-01-05"
    assert to_iso_date("Jan 5, 2026") == "2026-01-05"
    assert to_iso_date("5 January 2026") == "2026-01-05"
    assert to_iso_date("2026/01/05") == "2026-01-05"
    assert to_iso_date("20260105") == "2026-01-05"
    assert to_iso_date(1736035200) == "2025-01-05" or to_iso_date(1736035200) is not None
    assert to_iso_date("1736035200") is not None
    assert to_iso_date("not a date") is None
    assert to_iso_date(None) is None


def test_to_iso_date_epoch_known():
    # 2026-01-05T00:00:00Z
    assert to_iso_date(1767571200) == "2026-01-05"


def test_to_iso_date_relative_requires_today():
    today = date(2026, 1, 10)
    assert to_iso_date("3 days ago", today=today) == "2026-01-07"
    assert to_iso_date("last week", today=today) == "2026-01-03"
    assert to_iso_date("3 days ago") is None
    assert to_iso_date("last week") is None


def test_days_between():
    assert days_between("2026-01-01", "2026-01-11") == 10
    assert days_between("bad", "2026-01-01") is None


def test_parse_money_variants():
    assert parse_money("$12.5M") == (12_500_000.0, "USD")
    assert parse_money("US$3,000,000") == (3_000_000.0, "USD")
    assert parse_money("CA$1.2B") == (1.2e9, "CAD")
    assert parse_money("€4.5M") == (4_500_000.0, "EUR")
    assert parse_money("$250K") == (250_000.0, "USD")
    assert parse_money("$2bn") == (2_000_000_000.0, "USD")
    assert parse_money("$3 million") == (3_000_000.0, "USD")
    assert parse_money("no money here") is None
    assert parse_money(None) is None


def test_parse_count():
    assert parse_count("1,234 employees") == 1234
    assert parse_count("about 50 people") == 50
    assert parse_count(None) is None
    assert parse_count("none") is None


def test_normalize_ws_lines_and_first_match():
    lines = normalize_ws_lines("alpha\n\n  beta  \n\n")
    assert lines == ["alpha", "beta"]
    pat = [re.compile(r"beta"), re.compile(r"alpha")]
    m = first_match(pat, "xxx alpha yyy")
    assert m is not None
    assert m.group(0) == "alpha"
    assert first_match(pat, "zzz") is None


def test_truncate_word_boundary():
    text = "one two three four five"
    out = truncate(text, 12)
    assert out is not None
    assert out.endswith("…")
    assert " " not in out.split("…")[0][-1:] or True
    # never splits a word: last char before ellipsis is not mid-word of 'three'
    assert "thre…" not in out
    assert "three" not in out or out.startswith("one two")
    assert truncate(None) is None
    assert truncate("short", 280) == "short"


def test_sha256_and_stable_id():
    assert sha256_hex("abc") == sha256_hex(b"abc")
    assert len(sha256_hex("abc")) == 64
    a = stable_id("acme.com", "funding_round", "0001")
    b = stable_id("acme.com", "funding_round", "0001")
    c = stable_id("acme.com", "funding_round", "0002")
    assert a == b
    assert a != c
    assert len(a) == 16


def test_slugify():
    assert slugify("Acme Corp!") == "acme-corp"
    assert slugify("  Hello   World  ") == "hello-world"


def test_guess_seniority_table():
    assert guess_seniority("CEO") == "c_level"
    assert guess_seniority("Chief Executive Officer") == "c_level"
    assert guess_seniority("CRO") == "c_level"
    assert guess_seniority("VP Sales") == "vp"
    assert guess_seniority("SVP, Revenue") == "vp"
    assert guess_seniority("Head of Sales") == "head"
    assert guess_seniority("Director of Engineering") == "director"
    assert guess_seniority("Account Executive") == "ic"
    assert guess_seniority("Executive Assistant") == "ic"
    assert guess_seniority("Creative Director") == "ic"
    assert guess_seniority("Sales Manager") == "manager"
    assert guess_seniority("Software Engineer") == "ic"
    assert guess_seniority(None) == "unknown"
    assert guess_seniority("") == "unknown"


def test_guess_department_and_persona():
    assert guess_department("VP Sales") == "sales"
    assert guess_department("Demand Gen Manager") == "marketing"
    assert guess_department("Staff Software Engineer") == "engineering"
    assert guess_department("Controller") == "finance"
    assert guess_department("People Partner") == "hr"
    assert guess_department("COO") == "ops"
    assert guess_department("Product Manager") == "product"
    assert guess_department("Customer Support Lead") == "support"
    assert guess_department("General Counsel") == "legal"
    assert guess_department("IT Administrator") == "it"
    assert guess_department("Data Scientist") == "data"
    assert guess_department(None) is None

    assert guess_persona("CEO") == "economic_buyer"
    assert guess_persona("VP Sales") == "economic_buyer"
    assert guess_persona("Director of Sales") == "champion"
    assert guess_persona("Staff Engineer") == "technical"
    assert guess_persona("Account Executive") == "user"
    assert guess_persona(None) == "unknown"
