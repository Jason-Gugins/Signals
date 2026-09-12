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


def test_discover_writes_workday_compound_token(tmp_path):
    from src.core.db import Database
    from src.core.http import FetchResult
    from src.core.models import Account, Document
    from src.identity.ats_discovery import AtsDiscovery
    from src.identity.registry import AccountRegistry

    html = (FIXTURES / "careers_workday.html").read_text(encoding="utf-8")

    class Fake:
        def get(self, task, **kw):
            doc = Document(doc_id="c", source="ats_discovery", url=task.url, body=html.encode(), status=200)
            return FetchResult(True, 200, doc, False, None, 1)

    db = Database(tmp_path / "s.db")
    reg = AccountRegistry(db)
    acct = Account(domain="acme.com", careers_url="https://acme.com/careers")
    reg.upsert(acct)
    match = AtsDiscovery(Fake(), reg).discover(acct)
    stored = reg.get("acme.com")
    assert match.extra["wd"]
    assert stored.ats_vendor == "workday"
    assert stored.ats_token == f"{match.extra['tenant']}/{match.extra['wd']}/{match.extra['site']}"


CAREERS_HTML_LEVER = (
    '<html><body><a href="https://jobs.lever.co/acme">Open roles</a></body></html>'
)
SITEMAP_INDEX = (
    "<sitemapindex><sitemap>"
    "<loc>https://acme.com/sitemap-jobs.xml</loc>"
    "</sitemap></sitemapindex>"
)
SITEMAP_CAREERS = (
    "<urlset><url><loc>https://acme.com/company/careers</loc></url></urlset>"
)
ROBOTS_TXT = "Sitemap: https://acme.com/sitemap.xml\n"


def _ats_discovery(tmp_path, pages: dict):
    from src.core.db import Database
    from src.core.http import FetchResult
    from src.core.models import Account, Document
    from src.identity.ats_discovery import AtsDiscovery
    from src.identity.registry import AccountRegistry

    class Fake:
        def __init__(self):
            self.seen: list[str] = []

        def get(self, task, **kw):
            self.seen.append(task.url)
            body = pages.get(task.url)
            if body is None:
                return FetchResult(False, 404, None, False, "HTTP 404", 1)
            doc = Document(
                doc_id=task.url,
                source=task.source,
                url=task.url,
                body=body.encode(),
                status=200,
            )
            return FetchResult(True, 200, doc, False, None, 1)

    db = Database(tmp_path / "s.db")
    reg = AccountRegistry(db)
    acct = Account(domain="acme.com")
    reg.upsert(acct)
    fake = Fake()
    return AtsDiscovery(fake, reg), reg, acct, fake


def test_discover_uses_sitemap_before_guessing(tmp_path):
    pages = {
        "https://acme.com/robots.txt": ROBOTS_TXT,
        "https://acme.com/sitemap.xml": SITEMAP_INDEX,
        "https://acme.com/sitemap-jobs.xml": SITEMAP_CAREERS,
        "https://acme.com/company/careers": CAREERS_HTML_LEVER,
    }
    disc, reg, acct, fake = _ats_discovery(tmp_path, pages)
    match = disc.discover(acct)
    assert match is not None and match.vendor == "lever"
    stored = reg.get("acme.com")
    assert stored.ats_vendor == "lever"
    assert stored.ats_token == "acme"
    assert stored.careers_url == "https://acme.com/company/careers"
    # the sitemap path is preferred: no hardcoded guess was requested
    assert "https://acme.com/careers" not in fake.seen


def test_discover_persists_sitemap_careers_url_without_ats(tmp_path):
    pages = {
        "https://acme.com/robots.txt": ROBOTS_TXT,
        "https://acme.com/sitemap.xml": SITEMAP_CAREERS,
        "https://acme.com/company/careers": "<html><body>No openings</body></html>",
        "https://acme.com/": "<html><body>Home</body></html>",
    }
    disc, reg, acct, fake = _ats_discovery(tmp_path, pages)
    assert disc.discover(acct) is None
    stored = reg.get("acme.com")
    assert stored.careers_url == "https://acme.com/company/careers"
    assert not stored.ats_vendor and not stored.ats_token


def test_discover_known_careers_url_skips_sitemap(tmp_path):
    pages = {
        "https://acme.com/old-careers": "<html><body>Nothing</body></html>",
        "https://acme.com/": "<html><body>Nothing</body></html>",
    }
    disc, reg, acct, fake = _ats_discovery(tmp_path, pages)
    acct.careers_url = "https://acme.com/old-careers"
    reg.upsert(acct)
    assert disc.discover(reg.get("acme.com")) is None
    assert "https://acme.com/robots.txt" not in fake.seen
    assert reg.get("acme.com").careers_url == "https://acme.com/old-careers"


def test_discover_keeps_stored_careers_url_when_both_fetches_fail(tmp_path):
    pages = {
        "https://acme.com/robots.txt": ROBOTS_TXT,
        "https://acme.com/sitemap.xml": SITEMAP_INDEX,
        "https://acme.com/sitemap-jobs.xml": SITEMAP_CAREERS,
    }
    disc, reg, acct, fake = _ats_discovery(tmp_path, pages)
    acct.careers_url = "https://acme.com/good-careers"
    reg.upsert(acct)
    assert disc.discover(reg.get("acme.com")) is None
    assert reg.get("acme.com").careers_url == "https://acme.com/good-careers"


def test_discover_keeps_careers_index_when_ats_link_is_on_homepage(tmp_path):
    pages = {
        "https://acme.com/robots.txt": ROBOTS_TXT,
        "https://acme.com/sitemap.xml": SITEMAP_CAREERS,
        "https://acme.com/company/careers": "<html><body>No openings</body></html>",
        "https://acme.com/": CAREERS_HTML_LEVER,
    }
    disc, reg, acct, fake = _ats_discovery(tmp_path, pages)
    match = disc.discover(acct)
    assert match is not None and match.vendor == "lever"
    stored = reg.get("acme.com")
    assert stored.ats_vendor == "lever"
    assert stored.careers_url == "https://acme.com/company/careers"


def test_detect_ats_rejects_reserved_host_token():
    from src.identity.ats_discovery import detect_ats

    assert detect_ats('<a href="https://apply.workable.com">Apply</a>', "https://acme.com/careers") == []
    hits = detect_ats(
        '<a href="https://apply.workable.com/citylitics/">Open roles</a>',
        "https://acme.com/careers",
    )
    assert [(m.vendor, m.token) for m in hits] == [("workable", "citylitics")]


def test_detect_ats_reserved_tokens_are_generic():
    from src.identity.ats_discovery import detect_ats

    assert detect_ats('<a href="https://jobs.teamtailor.com/x">x</a>', "https://acme.com/careers") == []
    kept = detect_ats('<a href="https://acme.teamtailor.com/">x</a>', "https://acme.com/careers")
    assert [(m.vendor, m.token) for m in kept] == [("teamtailor", "acme")]


def test_discover_stamps_real_workable_token_and_keeps_careers_host(tmp_path):
    pages = {
        "https://acme.com/robots.txt": "",
        "https://acme.com/sitemap.xml": "",
        "https://acme.com/": '<html><a href="https://apply.workable.com/acme/">Open roles</a></html>',
        "https://careers.acme.com/": (
            '<html><a href="https://apply.workable.com/acme/">Open roles</a></html>'
        ),
    }
    disc, reg, acct, fake = _ats_discovery(tmp_path, pages)
    disc.discover(acct)
    stored = reg.get("acme.com")
    assert stored.ats_vendor == "workable"
    assert stored.ats_token == "acme"
    assert stored.careers_url == "https://careers.acme.com/"


def test_discover_persists_homepage_hop_url_without_ats(tmp_path):
    pages = {
        "https://acme.com/robots.txt": "",
        "https://acme.com/sitemap.xml": "",
        "https://acme.com/": '<html><a href="/company/careers">Careers</a></html>',
        "https://acme.com/company/careers": "<html><body>No openings listed</body></html>",
    }
    disc, reg, acct, fake = _ats_discovery(tmp_path, pages)
    assert disc.discover(acct) is None
    stored = reg.get("acme.com")
    assert stored.careers_url == "https://acme.com/company/careers"
    assert not stored.ats_vendor and not stored.ats_token


def test_discover_never_replaces_a_working_careers_url_without_ats(tmp_path):
    pages = {
        "https://acme.com/good-careers": "<html><body>No openings listed</body></html>",
        "https://acme.com/": '<html><a href="/careers">Careers</a></html>',
        "https://acme.com/careers": "<html><body>Another careers page</body></html>",
    }
    disc, reg, acct, fake = _ats_discovery(tmp_path, pages)
    acct.careers_url = "https://acme.com/good-careers"
    reg.upsert(acct)
    assert disc.discover(reg.get("acme.com")) is None
    assert reg.get("acme.com").careers_url == "https://acme.com/good-careers"
