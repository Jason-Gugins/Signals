"""Tests for ATS vendor/token discovery from HTML fixtures."""

from __future__ import annotations

import json
import re
from pathlib import Path

from src.identity.ats_discovery import (
    MAX_BOARD_CANDIDATES,
    MAX_VERIFY_REQUESTS,
    _LADDER_VENDORS,
    _VERIFY_ENDPOINTS,
    _board_payload_ok,
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
    # sub-3-char tokens are collision noise: the bare word "ai" is dropped
    assert "ai" not in cands
    # no dupes, all lowercase alnum
    assert len(cands) == len(set(cands))
    assert all(re.fullmatch(r"[a-z0-9_-]+", c) for c in cands)


def test_board_token_candidates_includes_aliases():
    cands = board_token_candidates("Abnormal AI", "abnormal.ai",
                                   aliases=["Abnormal Security", "Abnormal Security Ltd."])
    assert "abnormalsecurity" in cands     # THE case: former brand name
    assert "security" in cands             # individual words of multi-word aliases


def test_board_token_candidates_alias_before_name():
    """Alias joined-forms outrank current-name variants (diagonal round 2 must
    reach the former-brand token)."""
    cands = board_token_candidates("Abnormal AI", "abnormal.ai",
                                   aliases=["Abnormal Security"])
    assert cands[0] == "abnormal"            # domain prefix still first
    assert cands[1] == "abnormalsecurity"    # alias joined — round 2 token
    assert "abnormalai" in cands             # name joined — later round
    assert cands.index("abnormalsecurity") < cands.index("abnormalai")


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


def test_verify_board_candidates_never_raises_on_hostile_payload():
    """Documented contract: verify_board_candidates never raises. The payload
    predicate runs OUTSIDE the fetch try, so a hostile JSON body must be a
    MISS — totalFound = 1e999 parses to float inf and int(inf) overflows."""
    fetch = FakeJsonFetcher({
        "https://api.smartrecruiters.com/v1/companies/x/postings": '{"totalFound": 1e999}',
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


def test_discover_hub_hop_skips_self_linking_hub(tmp_path):
    """A careers page whose hub link points back at itself must not be
    refetched — the hub hop only spends its request on a NEW url. The path
    must not collide with the hardcoded Stage-4 candidates (/careers, /jobs,
    /join-us, ...), so /vacancies it is."""
    pages = {
        "https://acme.com/vacancies": '<html><body><a href="/vacancies">All roles</a></body></html>',
    }
    disc, reg, acct, fake = _ats_discovery(tmp_path, pages)
    acct.careers_url = "https://acme.com/vacancies"
    reg.upsert(acct)
    assert disc.discover(reg.get("acme.com")) is None
    # fetched once as the stored careers page, never again by the hub hop
    assert fake.seen.count("https://acme.com/vacancies") == 1


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


def test_discover_ladder_hit_never_stamps_site_root(tmp_path):
    """Root guard: a ladder hit with NO stored careers_url must not stamp the
    bare site root (here the only fetched page) as careers_url."""
    pages = {
        "https://acme.com/": "<html><body>we are hiring</body></html>",
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs": '{"jobs":[{"id":1}]}',
    }
    disc, reg, acct, fake = _ats_discovery(tmp_path, pages)
    match = disc.discover(acct, alias_names=[])
    assert match and match.vendor == "greenhouse" and match.token == "acme"
    stored = reg.get("acme.com")
    assert stored.ats_vendor == "greenhouse"
    # the homepage root was the only careers-ish page seen — it is NOT one
    assert stored.careers_url is None


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


def test_discover_ladder_probe_order_is_diagonal(tmp_path):
    """Diagonal round-robin (see the Stage 6 comment): token 1 across ALL
    pinned vendors, then token 2, budget-capped. Alias-derived token
    'abnormalsecurity' is round-2 probe #1 — overall #9 with the 8 pinned
    vendors — so the showcase stays reachable AND ashby/workable/
    smartrecruiters now see the domain-prefix token, which the old
    vendor-major order starved deterministically (8 probes bought
    greenhouse x 6 + lever x 2)."""
    pages = {"https://abnormal.ai/careers": "<html><body>we are hiring</body></html>"}
    # canned API hits: nothing verifies -> pure order assertion
    disc, reg, acct, fake = _ats_discovery(tmp_path, pages)
    acct.domain, acct.name = "abnormal.ai", "Abnormal AI"
    acct.careers_url = "https://abnormal.ai/careers"
    reg.upsert(acct)
    assert disc.discover(
        reg.get("abnormal.ai"),
        alias_names=["Abnormal Security", "Abnormal Security Ltd."],
    ) is None
    probes = fake.seen[-MAX_VERIFY_REQUESTS:]
    # Round 1 opens with the majors seeing the domain-prefix token...
    assert probes[:5] == [
        "https://boards-api.greenhouse.io/v1/boards/abnormal/jobs",
        "https://api.lever.co/v0/postings/abnormal?mode=json",
        "https://api.ashbyhq.com/posting-api/job-board/abnormal",
        "https://apply.workable.com/api/v1/widget/accounts/abnormal?details=true",
        "https://api.smartrecruiters.com/v1/companies/abnormal/postings",
    ]
    # ...and round 1 is the whole pinned rotation; round-2 probe #1 is
    # greenhouse with the alias token. n = len(_LADDER_VENDORS) keeps this
    # correct if vendors are appended later (8 today -> overall probe #9).
    n = len(_LADDER_VENDORS)
    assert probes[:n] == [
        _VERIFY_ENDPOINTS[v].format(t="abnormal") for v in _LADDER_VENDORS
    ]
    assert probes[n] == "https://boards-api.greenhouse.io/v1/boards/abnormalsecurity/jobs"
    # Round-2 tail pinned: the 12-probe budget buys only the first 4 probes of
    # round 2 — the majors in ladder order on the round-2 (alias) token. At
    # today's cap breezy/recruitee/teamtailor never see depth-2 tokens (see
    # the Stage 6 budget comment).
    assert probes[n : n + 4] == [
        _VERIFY_ENDPOINTS[v].format(t="abnormalsecurity")
        for v in ("greenhouse", "lever", "ashby", "workable")
    ]


def test_discover_ladder_diagonal_hit_on_round2(tmp_path):
    """(greenhouse, abnormalsecurity) verifies on round 2 -> stamped. The
    full round-1 rotation must fire before the hit: the showcase is reached
    by the diagonal, not by the old vendor-major probe #2."""
    pages = {
        "https://abnormal.ai/careers": "<html>hiring</html>",
        "https://boards-api.greenhouse.io/v1/boards/abnormalsecurity/jobs":
            '{"jobs": [{"title": "AE", "absolute_url": "https://abnormal.ai/careers/jobs/1"}]}',
    }
    disc, reg, acct, fake = _ats_discovery(tmp_path, pages)
    acct.domain, acct.name = "abnormal.ai", "Abnormal AI"
    acct.careers_url = "https://abnormal.ai/careers"
    reg.upsert(acct)
    match = disc.discover(
        reg.get("abnormal.ai"),
        alias_names=["Abnormal Security", "Abnormal Security Ltd."],
    )
    assert match and match.vendor == "greenhouse" and match.token == "abnormalsecurity"
    assert "candidate_ladder" in match.extra.get("source", "")
    stored = reg.get("abnormal.ai")
    assert stored.ats_vendor == "greenhouse"
    assert stored.ats_token == "abnormalsecurity"
    assert stored.careers_url == "https://abnormal.ai/careers"
    # probe path: the whole round-1 rotation on the domain-prefix token, THEN
    # the hit — round-2 probe #1, overall #9 with 8 pinned vendors.
    n = len(_LADDER_VENDORS)
    probes = fake.seen[-(n + 1):]
    assert probes[:n] == [
        _VERIFY_ENDPOINTS[v].format(t="abnormal") for v in _LADDER_VENDORS
    ]
    assert probes[-1] == "https://boards-api.greenhouse.io/v1/boards/abnormalsecurity/jobs"


def test_verify_board_endpoints_module_invariants():
    """Module-level pins: ashby verifies via the posting-api JSON ONLY (the
    jobs.ashbyhq.com HTML board returns an identical 200 SPA shell for real
    and garbage tokens alike), and the ladder covers only vendors with a
    keyless verifiable JSON API — workday/jobvite/rippling stay out. The
    diagonal rotation tuple must match the endpoint map exactly and contain
    only collector-backed vendors."""
    from src.sources.registry import COLLECTED_VENDORS

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
    # Diagonal rotation (Stage 6): every entry has a verify endpoint AND a
    # collector, and the tuple covers the endpoint map exactly.
    assert set(_LADDER_VENDORS) == set(_VERIFY_ENDPOINTS)
    assert set(_LADDER_VENDORS) <= COLLECTED_VENDORS


# --- Live-probe-pinned predicates (2026-10-04) ------------------------------
# POS/NEG canned bodies from read-only GETs on 2026-10-04 (UA "Mozilla/5.0
# (Windows NT 10.0; Win64; x64)", 15s timeouts, 8 requests total):
#   breezy     POS euler.breezy.hr/json -> 200 application/json, top-level
#              JSON list (19 items; item keys id/friendly_id/name/url/
#              published_date/type/location/department/salary/company/
#              locations — values below abbreviated)
#              NEG notarealboardxyz123 -> 404 text/html SPA shell, not JSON
#   recruitee  POS tether.recruitee.com/api/offers/ -> 200 application/json,
#              top-level dict with a single "offers" list (Tether's official
#              careers board; offer fields id/position/title/country/
#              published_at/...)
#              NEG notarealboardxyz123 -> 404 application/json {"error": ...}
#   teamtailor POS recruitgo.teamtailor.com/jobs.json -> 200 application/
#              feed+json, JSON Feed 1.1: {"version": ..., "title": ...,
#              "items": [41 jobs]} — NOT a "jobs" wrapper
#              NEG notarealboardxyz123 -> 404 application/json, EMPTY body


def test_board_payload_ok_breezy_live_pinned():
    """breezy pinned 2026-10-04 (POS euler): the real board payload is a
    top-level JSON list; parse_breezy also reads {"positions"/"jobs": [...]}
    dicts, so the predicate matches the parser. The 404 NEG body is an HTML
    SPA shell that json.loads must reject, and an empty list is no board."""
    pos = json.dumps(
        [
            {
                "id": "64f0c9e2a1b3",
                "friendly_id": "senior-backend-engineer",
                "name": "Senior Backend Engineer",
                "url": "https://euler.breezy.hr/senior-backend-engineer",
                "published_date": "2026-09-30T10:12:34.567+00:00",
                "type": {"id": "fullTime", "name": "Full-Time"},
                "location": {"name": "Remote", "is_remote": True},
                "department": "Engineering",
                "salary": "",
                "company": "Euler",
                "locations": [],
            }
        ]
    )
    assert _board_payload_ok("breezy", pos)
    assert _board_payload_ok("breezy", '{"positions": [{"id": "x"}]}')
    # NEG: live 404 body is an HTML SPA shell — not JSON.
    neg = (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta http-equiv="X-UA-Compatible" content="IE=edge">'
        '<meta name="viewport" content="width=device-width"></head>'
        "<body></body></html>"
    )
    assert not _board_payload_ok("breezy", neg)
    assert not _board_payload_ok("breezy", "[]")
    assert not _board_payload_ok("breezy", '{"error": "not found"}')
    assert not _board_payload_ok("breezy", "")


def test_board_payload_ok_recruitee_live_pinned():
    """recruitee pinned 2026-10-04 (POS tether): the real /api/offers/
    payload wraps jobs under "offers" — the ONLY key parse_recruitee reads,
    so the predicate must too (a real board carries no "jobs" key). The 404
    NEG is a JSON {"error": ...} dict, which must never verify."""
    pos = json.dumps(
        {
            "offers": [
                {
                    "id": 3774,
                    "position": 3774,
                    "title": "Event Design Coordinator",
                    "country": "Netherlands",
                    "state_name": "Noord-Holland",
                    "postal_code": None,
                    "published_at": "2026-10-01 14:34:00",
                    "careers_url": "https://tether.recruitee.com/o/event-design-coordinator",
                }
            ]
        }
    )
    assert _board_payload_ok("recruitee", pos)
    # NEG: live 404 shape is {"error": "..."} (application/json).
    neg = json.dumps({"error": "Not found"})
    assert not _board_payload_ok("recruitee", neg)
    # A jobs-wrapped body is not this endpoint's shape and parse_recruitee
    # cannot read it — stay strict.
    assert not _board_payload_ok("recruitee", '{"jobs": [{"id": 1}]}')
    assert not _board_payload_ok("recruitee", json.dumps({"offers": []}))
    assert not _board_payload_ok("recruitee", "")


def test_board_payload_ok_teamtailor_live_pinned():
    """teamtailor pinned 2026-10-04 (POS recruitgo): {t}.teamtailor.com/
    jobs.json returns a JSON Feed 1.1 document — jobs live under "items",
    which the old predicate (dict "jobs" only) silently REJECTED, making the
    ladder entry dead. The embedded board-state "jobs" shape that
    parse_teamtailor reads stays accepted. NEG: 404 with an EMPTY body."""
    pos = json.dumps(
        {
            "version": "https://jsonfeed.org/version/1.1",
            "title": "RecruitGo",
            "home_page_url": "https://recruitgo.teamtailor.com/jobs",
            "feed_url": "https://recruitgo.teamtailor.com/jobs.json",
            "items": [
                {
                    "id": "8ec986c9-5743-4a5b-ba81-fc4b1e0a2a61",
                    "title": "Senior Business Consultant",
                    "url": "https://recruitgo.teamtailor.com/jobs/8495559-senior-business-consultant",
                    "date_published": "2026-10-04T21:02:37+08:00",
                    "content_html": "<p><strong>About Us:</strong></p>",
                }
            ],
        }
    )
    assert _board_payload_ok("teamtailor", pos)
    assert _board_payload_ok("teamtailor", '{"jobs": [{"id": 1}]}')
    # NEG: live 404 body is empty (application/json, zero bytes).
    assert not _board_payload_ok("teamtailor", "")
    assert not _board_payload_ok(
        "teamtailor", '{"version": "https://jsonfeed.org/version/1.1", "items": []}'
    )
    assert not _board_payload_ok("teamtailor", "<html>not json</html>")


def test_verify_board_candidates_pinned_vendors_end_to_end():
    """The three probe-pinned vendors verify through the real ladder: the
    canned POS bodies from the 2026-10-04 live probes stamp; the canned NEG
    bodies do not."""
    fetch = FakeJsonFetcher(
        {
            "https://euler.breezy.hr/json": '[{"id": "64f0c9e2a1b3", "name": "Senior Backend Engineer"}]',
            "https://notarealboardxyz123.breezy.hr/json": "<!DOCTYPE html><html><body>404</body></html>",
            "https://tether.recruitee.com/api/offers/": '{"offers": [{"id": 3774, "title": "Event Design Coordinator"}]}',
            "https://notarealboardxyz123.recruitee.com/api/offers/": '{"error": "Not found"}',
            "https://recruitgo.teamtailor.com/jobs.json": '{"version": "https://jsonfeed.org/version/1.1", "items": [{"id": "x", "title": "SB"}]}',
            "https://notarealboardxyz123.teamtailor.com/jobs.json": "",
        }
    )
    pos = verify_board_candidates(
        fetch,
        [("breezy", "euler"), ("recruitee", "tether"), ("teamtailor", "recruitgo")],
    )
    assert pos and pos.vendor == "breezy" and pos.token == "euler"
    neg = verify_board_candidates(
        fetch,
        [
            ("breezy", "notarealboardxyz123"),
            ("recruitee", "notarealboardxyz123"),
            ("teamtailor", "notarealboardxyz123"),
        ],
    )
    assert neg is None


# --- VERIFIER->PARSER round-trip ---------------------------------------------
# Canned POS bodies = the 2026-10-04 live probe captures (same key shapes the
# pinned predicate tests above record).

BREEZY_EULER_POS = json.dumps(
    [
        {
            "id": "64f0c9e2a1b3",
            "friendly_id": "senior-backend-engineer",
            "name": "Senior Backend Engineer",
            "url": "https://euler.breezy.hr/senior-backend-engineer",
            "published_date": "2026-09-30T10:12:34.567+00:00",
            "type": {"id": "fullTime", "name": "Full-Time"},
            "location": {"name": "Remote", "is_remote": True},
            "department": "Engineering",
            "salary": "",
            "company": "Euler",
            "locations": [],
        }
    ]
)
RECRUITEE_TETHER_POS = json.dumps(
    {
        "offers": [
            {
                "id": 3774,
                "position": 3774,
                "title": "Event Design Coordinator",
                "country": "Netherlands",
                "state_name": "Noord-Holland",
                "postal_code": None,
                "published_at": "2026-10-01 14:34:00",
                "careers_url": "https://tether.recruitee.com/o/event-design-coordinator",
            }
        ]
    }
)
TEAMTAILOR_RECRUITGO_POS = json.dumps(
    {
        "version": "https://jsonfeed.org/version/1.1",
        "title": "RecruitGo",
        "home_page_url": "https://recruitgo.teamtailor.com/jobs",
        "feed_url": "https://recruitgo.teamtailor.com/jobs.json",
        "items": [
            {
                "id": "8ec986c9-5743-4a5b-ba81-fc4b1e0a2a61",
                "title": "Senior Business Consultant",
                "url": "https://recruitgo.teamtailor.com/jobs/8495559-senior-business-consultant",
                "date_published": "2026-10-04T21:02:37+08:00",
                "content_html": "<p><strong>About Us:</strong></p>",
            }
        ],
    }
)


def test_verifier_parser_round_trip_all_pinned_vendors():
    """Whatever _board_payload_ok accepts, the SAME vendor's parser must turn
    into >=1 JobPost. Each pinned collector fetches the SAME URL the verifier
    probes (TeamtailorSource.plan -> {t}.teamtailor.com/jobs.json), so a
    verifier-accepted payload the parser cannot read stamps the account with
    a collector that collects nothing — and the stamp suppresses the
    ats_careers_page fallback that would otherwise have collected."""
    from src.sources.ats.breezy import parse_breezy
    from src.sources.ats.recruitee import parse_recruitee
    from src.sources.ats.teamtailor import parse_teamtailor

    cases = [
        ("breezy", BREEZY_EULER_POS, parse_breezy),
        ("recruitee", RECRUITEE_TETHER_POS, parse_recruitee),
        ("teamtailor", TEAMTAILOR_RECRUITGO_POS, parse_teamtailor),
    ]
    for vendor, body, parse in cases:
        assert _board_payload_ok(vendor, body), vendor
        jobs = parse(body.encode("utf-8"))
        assert len(jobs) > 0, (
            f"{vendor}: verifier accepts the live payload but the parser yielded 0 jobs"
        )
    # Field mapping on the live teamtailor JSON Feed item.
    job = parse_teamtailor(TEAMTAILOR_RECRUITGO_POS.encode("utf-8"))[0]
    assert job.title == "Senior Business Consultant"
    assert job.url == "https://recruitgo.teamtailor.com/jobs/8495559-senior-business-consultant"
    assert job.external_id == "8ec986c9-5743-4a5b-ba81-fc4b1e0a2a61"
    assert job.posted_at == "2026-10-04"  # date_published via to_iso_date
    assert job.description == "About Us:"  # strip_html(content_html)
