"""`signals find-careers` wiring."""

from __future__ import annotations

from click.testing import CliRunner

from src.cli import main
from src.identity.sitemap_careers import CareersLookup
from src.pipeline.orchestrator import Orchestrator


def test_find_careers_prints_url(monkeypatch):
    def find_careers(self, domain):
        assert domain == "acme.com"
        return CareersLookup(
            careers_url="https://acme.com/company/careers",
            source="robots_sitemap",
            requests=4,
            pages_seen=812,
        )

    monkeypatch.setattr(Orchestrator, "find_careers", find_careers)
    result = CliRunner().invoke(main, ["find-careers", "acme.com"])
    assert result.exit_code == 0
    assert "careers_url=https://acme.com/company/careers" in result.output
    assert "source=robots_sitemap" in result.output


def test_find_careers_exits_nonzero_when_missing(monkeypatch):
    monkeypatch.setattr(
        Orchestrator, "find_careers", lambda self, domain: CareersLookup()
    )
    result = CliRunner().invoke(main, ["find-careers", "acme.com"])
    assert result.exit_code == 1
    assert "careers_url=" in result.output
