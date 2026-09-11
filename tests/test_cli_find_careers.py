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


def test_orchestrator_find_careers_wiring(tmp_path):
    """The real Orchestrator.find_careers path, with an injected fake fetcher."""
    from src.core.db import Database
    from src.core.http import FetchResult
    from src.core.models import Document
    from src.pipeline.orchestrator import Orchestrator
    from src.core.config import Config

    pages = {
        "https://acme.com/robots.txt": "Sitemap: https://acme.com/sitemap.xml\n",
        "https://acme.com/sitemap.xml": (
            "<urlset><url><loc>https://acme.com/company/careers</loc></url></urlset>"
        ),
    }

    class Fake:
        def __init__(self):
            self.seen = []

        def get(self, task, **kw):
            self.seen.append(task.url)
            body = pages.get(task.url)
            if body is None:
                return FetchResult(False, 404, None, False, "HTTP 404", 1)
            doc = Document(
                doc_id=task.url, source=task.source, url=task.url, body=body.encode(), status=200
            )
            return FetchResult(True, 200, doc, False, None, 1)

    fake = Fake()
    orch = Orchestrator(Config.load(), db=Database(tmp_path / "w.db"), fetcher=fake)
    lookup = orch.find_careers("acme.com")
    assert lookup.careers_url == "https://acme.com/company/careers"
    assert lookup.source == "robots_sitemap"
    assert fake.seen[0] == "https://acme.com/robots.txt"


def test_find_careers_url_falls_back_to_homepage_link():
    from src.identity.ats_discovery import find_careers_url

    def fetch(url):
        return {
            "https://acme.com/robots.txt": "",
            "https://acme.com/sitemap.xml": "",
            "https://acme.com/": '<html><a href="/careers">Careers</a></html>',
        }.get(url)

    lookup = find_careers_url(fetch, "acme.com")
    assert lookup.careers_url == "https://acme.com/careers"
    assert lookup.source == "homepage_link"


def test_find_careers_url_falls_back_to_candidates():
    from src.identity.ats_discovery import find_careers_url

    def fetch(url):
        return {
            "https://acme.com/robots.txt": "",
            "https://acme.com/sitemap.xml": "",
            "https://acme.com/": "<html><body>No links here</body></html>",
            "https://acme.com/jobs": "<html><body>Jobs</body></html>",
        }.get(url)

    lookup = find_careers_url(fetch, "acme.com")
    assert lookup.careers_url == "https://acme.com/jobs"
    assert lookup.source == "candidate"


def test_find_careers_url_prefers_sitemap():
    from src.identity.ats_discovery import find_careers_url

    def fetch(url):
        return {
            "https://acme.com/robots.txt": "",
            "https://acme.com/sitemap.xml": (
                "<urlset><url><loc>https://acme.com/company/careers</loc></url></urlset>"
            ),
        }.get(url)

    lookup = find_careers_url(fetch, "acme.com")
    assert lookup.careers_url == "https://acme.com/company/careers"
    assert lookup.source == "root_sitemap"
