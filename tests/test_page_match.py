"""Tests for Form D issuer vs homepage match (pure)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from src.sources.sec.page_match import issuer_matches_page
from src.sources.sec.page_peel import PageHints, peel_page
from src.sources.sec.parse_formd import parse_form_d

PAGES = Path(__file__).parent / "fixtures" / "sec" / "pages"
_FD = parse_form_d((Path(__file__).parent / "fixtures" / "sec" / "form_d_primary_doc.xml").read_bytes())


def _fd(**kw):
    return replace(_FD, **kw)


def test_drops_surgical_safety_for_scanner():
    fd = _fd(entity_name="Surgical Safety Scanner, Inc.", city="Brighton")
    hints = peel_page((PAGES / "scanner.dev.html").read_bytes(), domain="scanner.dev")
    assert issuer_matches_page(fd, hints, brand="Scanner") is False


def test_keeps_radicl_defense():
    fd = _fd(entity_name="RADICL Defense, Inc.", city="BOULDER", industry_group="Other Technology")
    hints = peel_page((PAGES / "radicl.com.html").read_bytes(), domain="radicl.com")
    assert issuer_matches_page(fd, hints, brand="RADICL") is True


def test_keeps_beamable_inc():
    fd = _fd(entity_name="Beamable, Inc.", city="Boston")
    hints = peel_page((PAGES / "beamable.com.html").read_bytes(), domain="beamable.com")
    assert issuer_matches_page(fd, hints, brand="Beamable") is True


def test_drops_parallel_reit():
    fd = _fd(entity_name="CGMT Parallel REIT, L.P.", industry_group="REITS and Finance")
    hints = peel_page((PAGES / "parallel.ai.html").read_bytes(), domain="parallel.ai")
    assert issuer_matches_page(fd, hints, brand="Parallel") is False


def test_drops_parallel_loop_without_page_overlap():
    fd = _fd(entity_name="Parallel Loop, Inc.", city="SAN FRANCISCO")
    hints = peel_page((PAGES / "parallel.ai.html").read_bytes(), domain="parallel.ai")
    assert issuer_matches_page(fd, hints, brand="Parallel") is False


def test_empty_hints_still_drops_surgical_safety():
    empty = PageHints(titles=(), legal_names=(), site_names=(), cities=(), text_blob="")
    assert issuer_matches_page(_fd(entity_name="Surgical Safety Scanner, Inc."), empty, brand="Scanner") is False
