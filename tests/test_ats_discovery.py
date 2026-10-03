"""Tests for ATS vendor/token discovery from HTML fixtures."""

from __future__ import annotations

import re
from pathlib import Path

from src.identity.ats_discovery import (
    MAX_BOARD_CANDIDATES,
    MAX_VERIFY_REQUESTS,
    _VERIFY_ENDPOINTS,
    board_token_candidates,
    careers_url_candidates,
    detect_ats,
    listing_hub_link,
    verify_board_candidates,
)


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


def test_board_token_candidates_from_name_and_domain():
    # "Abnormal AI" + abnormal.ai -> joined slug, first word, domain prefix
    cands = board_token_candidates("Abnormal AI", "abnormal.ai", aliases=[])
    assert cands[0] == "abnormal"          # domain prefix first
    assert "abnormalai" in cands           # name joined
    # no dupes, all lowercase alnum
    assert len(cands) == len(set(cands))
    assert all(re.fullmatch(r"[a-z0-9_-]+", c) for c in cands)


def test_board_token_candidates_includes_aliases():
    cands = board_token_candidates("Abnormal AI", "abnormal.ai",
                                   aliases=["Abnormal Security", "Abnormal Security Ltd."])
    assert "abnormalsecurity" in cands     # THE case: former brand name
    assert "security" in cands             # individual words of multi-word aliases


def test_board_token_candidates_filters_reserved_and_caps():
    cands = board_token_candidates("Jobs", "jobs.io", aliases=["www", "api"])
    assert "jobs" not in cands and "www" not in cands and "api" not in cands
    assert len(cands) <= MAX_BOARD_CANDIDATES


def test_listing_hub_link_prefers_open_roles():
    html = """
    <a href="/careers/stories">Stories</a>
    <a href="/careers/open-roles?category=Engineering">Engineering roles</a>
    <a href="/careers/open-roles">Open roles</a>
    <a href="https://twitter.com/x">Social</a>
    """
    link = listing_hub_link(html, "https://abnormal.ai/careers")
    assert link == "https://abnormal.ai/careers/open-roles"   # query stripped, deduped


def test_listing_hub_link_none_when_no_hub():
    assert listing_hub_link("<a href='/about'>About</a>", "https://x.test/") is None
    # boundary trap: the hub token must be its own path segment or end the path,
    # so a careers index or a stories sub-page is never mistaken for the hub.
    assert (
        listing_hub_link("<a href='/careers/stories'>Stories</a>", "https://x.test/careers")
        is None
    )


def test_listing_hub_link_accepts_known_hub_shapes():
    for path in ("/jobs", "/open-positions", "/search-jobs", "/vacancies", "/join-us"):
        assert listing_hub_link(f'<a href="{path}">Roles</a>', "https://x.test/careers") == f"https://x.test{path}"


class FakeJsonFetcher:
    """Returns canned (status, body) per URL; records calls.

    A canned value may be a (status, body) tuple or a plain body string
    (implied 200, the JSON-API success shape these tests care about)."""
    def __init__(self, by_url):
        self.by_url, self.calls = dict(by_url), []

    def __call__(self, url):
        self.calls.append(url)
        canned = self.by_url.get(url, (404, ""))
        status, body = canned if isinstance(canned, tuple) else (200, canned)
        return body if status == 200 else None


def test_verify_board_candidates_greenhouse_hit():
    fetch = FakeJsonFetcher({
        "https://boards-api.greenhouse.io/v1/boards/abnormalsecurity/jobs":
            '{"jobs": [{"title": "AE"}]}',
    })
    cands = [("greenhouse", "abnormal"), ("greenhouse", "abnormalsecurity")]
    match = verify_board_candidates(fetch, cands)
    assert match and match.vendor == "greenhouse" and match.token == "abnormalsecurity"
    assert "candidate_ladder" in match.extra.get("source", "")


def test_verify_board_candidates_all_miss_is_none():
    fetch = FakeJsonFetcher({})  # everything 404s
    cands = [(v, "x") for v in ("greenhouse", "lever", "ashby", "workable", "smartrecruiters")]
    assert verify_board_candidates(fetch, cands) is None


def test_verify_board_candidates_smartrecruiters_needs_totalfound():
    fetch = FakeJsonFetcher({
        "https://api.smartrecruiters.com/v1/companies/x/postings": '{"totalFound":0,"content":[]}'
    })
    assert verify_board_candidates(fetch, [("smartrecruiters", "x")]) is None


def test_verify_board_candidates_ashby_never_trusts_html():
    """The ashby HTML board is a 200 catch-all — only posting-api JSON counts."""
    fetch = FakeJsonFetcher({
        "https://api.ashbyhq.com/posting-api/job-board/t": '{"jobs": [{"id": "1"}]}'
    })
    match = verify_board_candidates(fetch, [("ashby", "t")])
    assert match and match.token == "t"
    # and the URL list must never contain jobs.ashbyhq.com
    assert all("jobs.ashbyhq.com" not in u for u in fetch.calls)


def test_verify_board_candidates_vendor_order_and_cap():
    fetch = FakeJsonFetcher({
        "https://api.lever.co/v0/postings/spotify?mode=json": '[{"id": "1"}]'
    })
    cands = [("lever", "spotify"), ("greenhouse", "spotify"), ("lever", "other")]
    match = verify_board_candidates(fetch, cands)
    assert match and match.vendor == "lever" and match.token == "spotify"
    assert len(fetch.calls) <= MAX_VERIFY_REQUESTS


def test_discover_hub_hop_finds_vendor_on_listing_page(tmp_path):
    """Careers index is a marker-free shell; the hub page carries the marker."""
    pages = {
        "https://acme.com/careers": '<a href="/careers/open-roles">Open roles</a>',
        "https://acme.com/careers/open-roles": (
            '<script src="https://boards.greenhouse.io/embed/job_board?for=xtest"></script>'
        ),
    }
    disc, reg, acct, fake = _ats_discovery(tmp_path, pages)
    acct.careers_url = "https://acme.com/careers"
    reg.upsert(acct)
    match = disc.discover(reg.get("acme.com"))
    assert match and match.vendor == "greenhouse" and match.token == "xtest"
    stored = reg.get("acme.com")
    assert stored.ats_vendor == "greenhouse"
    assert stored.ats_token == "xtest"
    # the hub page was fetched exactly once and becomes the careers page
    assert fake.seen.count("https://acme.com/careers/open-roles") == 1
    assert stored.careers_url == "https://acme.com/careers/open-roles"


def test_discover_candidate_ladder_stamps_verified_alias_token(tmp_path):
    """No markers anywhere; the alias-derived token verified against the JSON API."""
    pages = {
        "https://acme.com/careers": "<p>we are hiring</p>",  # marker-free shell
        "https://boards-api.greenhouse.io/v1/boards/oldname/jobs": '{"jobs":[{"id":1}]}',
    }
    disc, reg, acct, fake = _ats_discovery(tmp_path, pages)
    acct.careers_url = "https://acme.com/careers"
    acct.name = "Acme Co"
    reg.upsert(acct)
    match = disc.discover(reg.get("acme.com"), alias_names=["Old Name"])
    assert match and match.vendor == "greenhouse" and match.token == "oldname"
    assert "candidate_ladder" in match.extra.get("source", "")
    stored = reg.get("acme.com")
    assert stored.ats_vendor == "greenhouse"
    assert stored.ats_token == "oldname"
    # a ladder hit never discovered a new careers page: the stored one stays
    assert stored.careers_url == "https://acme.com/careers"


def test_discover_ladder_miss_still_persists_careers_url(tmp_path):
    """A ladder miss degrades to today's behavior: no vendor/token stamped and
    a discovered careers index is still persisted for the fallback scraper."""
    pages = {
        "https://acme.com/robots.txt": "",
        "https://acme.com/sitemap.xml": "",
        "https://acme.com/": '<html><a href="/company/careers">Careers</a></html>',
        "https://acme.com/company/careers": "<html><body>No openings listed</body></html>",
    }
    disc, reg, acct, fake = _ats_discovery(tmp_path, pages)
    assert disc.discover(acct, alias_names=[]) is None
    stored = reg.get("acme.com")
    assert stored.careers_url == "https://acme.com/company/careers"
    assert not stored.ats_vendor and not stored.ats_token


def test_discover_candidate_ladder_skipped_after_marker_hit(tmp_path, monkeypatch):
    """Stage 6 probes vendor APIs only when no COLLECTED-vendor marker matched:
    a marker hit must never spend budget on the candidate ladder."""
    import src.identity.ats_discovery as ats_discovery_module

    pages = {"https://acme.com/careers": CAREERS_HTML_LEVER}
    disc, reg, acct, fake = _ats_discovery(tmp_path, pages)
    acct.careers_url = "https://acme.com/careers"
    reg.upsert(acct)

    def _no_ladder(fetch_text, candidates):
        raise AssertionError("candidate ladder ran after a marker match")

    monkeypatch.setattr(ats_discovery_module, "verify_board_candidates", _no_ladder)
    match = disc.discover(reg.get("acme.com"), alias_names=["Old Name"])
    assert match and match.vendor == "lever" and match.token == "acme"
    stored = reg.get("acme.com")
    assert stored.ats_vendor == "lever"


def test_verify_board_endpoints_module_invariants():
    """Module-level pins: ashby verifies via the posting-api JSON ONLY (the
    jobs.ashbyhq.com HTML board returns an identical 200 SPA shell for real
    and garbage tokens alike), and the ladder covers only vendors with a
    keyless verifiable JSON API — workday/jobvite/rippling stay out."""
    assert "ashby" in _VERIFY_ENDPOINTS
    assert all("jobs.ashbyhq.com" not in u for u in _VERIFY_ENDPOINTS.values())
    assert set(_VERIFY_ENDPOINTS) <= {
        "greenhouse",
        "lever",
        "ashby",
        "workable",
        "smartrecruiters",
        "breezy",
        "recruitee",
        "teamtailor",
    }
