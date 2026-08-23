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


def test_drops_momentum_llc():
    fd = _fd(entity_name="Momentum LLC", city="Sandy", industry_group="Retailing")
    hints = PageHints(
        titles=("Momentum: AI Revenue Orchestration Platform", "Momentum"),
        legal_names=(),
        site_names=("Momentum",),
        cities=(),
        text_blob="Momentum: AI Revenue Orchestration Platform AI Revenue Orchestration",
    )
    assert issuer_matches_page(fd, hints, brand="Momentum") is False


def test_drops_polymarket_fund_series():
    fd = _fd(
        entity_name="OPX2025 Polymarket, a Series of Opulentia Ventures X LLC",
        industry_group="Investing",
    )
    hints = PageHints(
        titles=("Polymarket | The World's Largest Prediction Market", "Polymarket"),
        legal_names=(),
        site_names=("Polymarket",),
        cities=(),
        text_blob="Polymarket The World's Largest Prediction Market",
    )
    assert issuer_matches_page(fd, hints, brand="Polymarket") is False


def test_drops_west_clay_and_red_clay():
    hints = PageHints(
        titles=("Clay | Build systems to grow revenue", "Clay"),
        legal_names=(),
        site_names=("Clay",),
        cities=(),
        text_blob="Clay Build systems to grow revenue",
    )
    assert issuer_matches_page(_fd(entity_name="OE VILLAGE OF WEST CLAY, LLC"), hints, brand="Clay") is False
    assert issuer_matches_page(_fd(entity_name="Red Clay Provisions, LLC"), hints, brand="Clay") is False


def test_keeps_agentio_inc():
    hints = PageHints(
        titles=("Agentio | The AI-Native Platform for Creator Advertising", "Agentio"),
        legal_names=(),
        site_names=("Agentio",),
        cities=(),
        text_blob="Agentio The AI-Native Platform for Creator Advertising",
    )
    assert issuer_matches_page(_fd(entity_name="Agentio Inc."), hints, brand="Agentio") is True


def test_keeps_speakeasy_labs_for_speak():
    hints = PageHints(
        titles=("Speak - The language learning app", "Speak"),
        legal_names=("Speakeasy Labs, Inc.",),
        site_names=("Speak",),
        cities=(),
        text_blob="Speak The language learning app Speakeasy Labs, Inc.",
    )
    assert issuer_matches_page(_fd(entity_name="Speakeasy Labs, Inc."), hints, brand="Speak") is True
