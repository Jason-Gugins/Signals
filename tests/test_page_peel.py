"""Tests for homepage legal-name peel (pure, no I/O)."""

from __future__ import annotations

from pathlib import Path

from src.sources.sec.page_peel import PageHints, peel_legal_names, peel_page

PAGES = Path(__file__).parent / "fixtures" / "sec" / "pages"


def test_scanner_copyright_inc():
    hints = peel_page((PAGES / "scanner.dev.html").read_bytes(), domain="scanner.dev")
    names = peel_legal_names(hints, domain="scanner.dev")
    assert any("scanner" in n.casefold() and "inc" in n.casefold() for n in names)
    assert all("surgical" not in n.casefold() for n in names)


def test_beamable_title_or_copyright():
    names = peel_legal_names(
        peel_page((PAGES / "beamable.com.html").read_bytes(), domain="beamable.com"),
        domain="beamable.com",
    )
    assert any("beamable" in n.casefold() for n in names)


def test_domain_label_fallback():
    hints = peel_page(b"<html><title></title></html>", domain="scanner.dev")
    names = peel_legal_names(hints, domain="scanner.dev")
    assert names[0].casefold() == "scanner"


def test_radicl_legal_name_not_fonticons():
    names = peel_legal_names(
        peel_page((PAGES / "radicl.com.html").read_bytes(), domain="radicl.com"),
        domain="radicl.com",
    )
    assert any("radicl" in n.casefold() and "defense" in n.casefold() for n in names)
    assert all("fonticons" not in n.casefold() for n in names)


def test_parallel_broken_jsonld_still_peels_name():
    names = peel_legal_names(
        peel_page((PAGES / "parallel.ai.html").read_bytes(), domain="parallel.ai"),
        domain="parallel.ai",
    )
    assert any("parallel" in n.casefold() for n in names)
    assert names


def test_peel_dedupes_inc_punctuation():
    hints = PageHints(
        titles=(),
        legal_names=("Speakeasy Labs, Inc.", "Speakeasy Labs, Inc"),
        site_names=(),
        cities=(),
        text_blob="",
    )
    names = peel_legal_names(hints, domain="speak.com")
    labs = [n for n in names if "speakeasy" in n.casefold()]
    assert len(labs) == 1
