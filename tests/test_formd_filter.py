"""Tests for Form D lead keep/drop filters."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from src.sources.sec.formd_filter import FormDFilter, amount_usd, keep_form_d
from src.sources.sec.parse_formd import parse_form_d


FIX = Path(__file__).parent / "fixtures" / "sec" / "form_d_primary_doc.xml"


def _fd(**kw):
    return replace(parse_form_d(FIX.read_bytes()), **kw)


def test_empty_filter_keeps_acme():
    fd = _fd()
    assert keep_form_d(fd, FormDFilter()) is True
    assert amount_usd(fd) == 20_000_000.0


def test_pooled_dropped_unless_flag():
    fd = _fd(industry_group="Pooled Investment Fund")
    assert keep_form_d(fd, FormDFilter()) is False
    assert keep_form_d(fd, FormDFilter(include_funds=True)) is True


def test_amendment_dropped_unless_flag():
    fd = _fd(is_amendment=True)
    assert keep_form_d(fd, FormDFilter()) is False
    assert keep_form_d(fd, FormDFilter(include_amendments=True)) is True


def test_min_sold_boundary():
    fd = _fd()
    assert keep_form_d(fd, FormDFilter(min_sold=20_000_000)) is True
    assert keep_form_d(fd, FormDFilter(min_sold=20_000_000.01)) is False
    sold_zero = _fd(total_sold=0.0, total_offering=5_000_000.0)
    assert amount_usd(sold_zero) == 5_000_000.0
    none_amt = _fd(total_sold=None, total_offering=None)
    assert keep_form_d(none_amt, FormDFilter(min_sold=1)) is False


def test_state_case_insensitive():
    fd = _fd(state="DE")
    assert keep_form_d(fd, FormDFilter(state="de")) is True
    assert keep_form_d(fd, FormDFilter(state="CA")) is False
