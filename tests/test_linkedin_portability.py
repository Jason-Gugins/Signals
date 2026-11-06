"""Portable sibling-scraper venv resolution (P5): Windows default unchanged,
POSIX layout for containers, explicit env override wins."""

from pathlib import Path

import src.sources.linkedin_db.collector as collector


def test_scraper_python_windows_layout_is_unchanged(monkeypatch):
    # Local-as-is contract: on Windows the resolved path is byte-identical
    # to the previous hardcoded expression.
    monkeypatch.delenv("SIGNALS_SCRAPER_PYTHON", raising=False)
    assert collector.scraper_python(Path("/checkouts/linkedin"), platform="nt") == Path(
        "/checkouts/linkedin/.venv/Scripts/python.exe"
    )


def test_scraper_python_posix_layout_for_containers(monkeypatch):
    monkeypatch.delenv("SIGNALS_SCRAPER_PYTHON", raising=False)
    assert collector.scraper_python(Path("/checkouts/linkedin"), platform="posix") == Path(
        "/checkouts/linkedin/.venv/bin/python"
    )


def test_scraper_python_env_override_wins(monkeypatch):
    monkeypatch.setenv("SIGNALS_SCRAPER_PYTHON", "/opt/special/python")
    assert collector.scraper_python(Path("/checkouts/linkedin"), platform="nt") == Path(
        "/opt/special/python"
    )
