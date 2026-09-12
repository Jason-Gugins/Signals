"""Careers-URL discovery from robots.txt + XML sitemaps."""

from __future__ import annotations

from src.identity.sitemap_careers import (
    SitemapCareersFinder,
    career_path_score,
    parse_robots_sitemaps,
    parse_sitemap,
    pick_careers_url,
)

ROBOTS = """User-agent: *
Disallow: /admin
Sitemap: https://acme.com/sitemap.xml
sitemap: /sitemap-jobs.xml
"""

URLSET = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://acme.com/</loc><lastmod>2026-09-01</lastmod></url>
  <url><loc>https://acme.com/careers</loc></url>
</urlset>
"""

INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://acme.com/sitemap-pages.xml</loc></sitemap>
  <sitemap><loc>https://acme.com/sitemap-jobs.xml</loc></sitemap>
</sitemapindex>
"""


class FakeFetch:
    """Injected fetch_text: records requested URLs, serves from a dict."""

    def __init__(self, pages: dict):
        self.pages = pages
        self.seen: list = []

    def __call__(self, url: str):
        self.seen.append(url)
        return self.pages.get(url)


def test_parse_robots_sitemaps_absolute_and_relative():
    got = parse_robots_sitemaps(ROBOTS, "https://acme.com/robots.txt")
    assert got == ["https://acme.com/sitemap.xml", "https://acme.com/sitemap-jobs.xml"]


def test_parse_robots_sitemaps_empty():
    assert parse_robots_sitemaps("", "https://acme.com/robots.txt") == []
    assert parse_robots_sitemaps(
        "User-agent: *\nDisallow: /\n", "https://acme.com/robots.txt"
    ) == []


def test_parse_sitemap_urlset():
    doc = parse_sitemap(URLSET)
    assert doc.kind == "urlset"
    assert doc.urls == ("https://acme.com/", "https://acme.com/careers")
    assert doc.sitemaps == ()


def test_parse_sitemap_index():
    doc = parse_sitemap(INDEX)
    assert doc.kind == "sitemapindex"
    assert doc.urls == ()
    assert doc.sitemaps == (
        "https://acme.com/sitemap-pages.xml",
        "https://acme.com/sitemap-jobs.xml",
    )


def test_parse_sitemap_text_and_unknown():
    doc = parse_sitemap("https://acme.com/careers\nhttps://acme.com/jobs\n")
    assert doc.kind == "text"
    assert doc.urls == ("https://acme.com/careers", "https://acme.com/jobs")
    junk = parse_sitemap("\x1f\x8b\x08junk-not-xml")
    assert junk.kind == "unknown"
    assert junk.urls == () and junk.sitemaps == ()


def test_career_path_score_prefers_index_over_detail():
    assert career_path_score("https://acme.com/careers") > 0
    assert career_path_score("https://acme.com/company/careers") > 0
    assert career_path_score("https://acme.com/careers") > career_path_score(
        "https://acme.com/careers/senior-account-executive-1234567"
    )
    assert career_path_score("https://acme.com/blog/why-we-love-sales") < 0
    assert career_path_score("https://acme.com/") < 0
    assert career_path_score("https://acme.com/pricing") < 0


def test_pick_careers_url_prefers_shallow_and_is_stable():
    pool = [
        "https://acme.com/blog/post",
        "https://acme.com/careers/senior-ae-1234567",
        "https://acme.com/jobs",
        "https://acme.com/careers",
    ]
    assert pick_careers_url(pool) == "https://acme.com/careers"
    assert pick_careers_url(reversed(pool)) == "https://acme.com/careers"


def test_pick_careers_url_none_without_signal():
    assert pick_careers_url(["https://acme.com/", "https://acme.com/pricing"]) is None
    assert pick_careers_url([]) is None


def test_finder_walks_robots_then_index_then_careers_child():
    fetch = FakeFetch(
        {
            "https://acme.com/robots.txt": "Sitemap: https://acme.com/sitemap.xml\n",
            "https://acme.com/sitemap.xml": INDEX,
            "https://acme.com/sitemap-pages.xml": URLSET,
        }
    )
    lookup = SitemapCareersFinder(fetch).find("acme.com")
    assert lookup.careers_url == "https://acme.com/careers"
    assert lookup.source == "robots_sitemap"
    assert fetch.seen[0] == "https://acme.com/robots.txt"
    # career-ish child sitemap is tried before the generic pages sitemap
    assert fetch.seen.index("https://acme.com/sitemap-jobs.xml") < fetch.seen.index(
        "https://acme.com/sitemap-pages.xml"
    )


def test_finder_falls_back_to_root_sitemap_without_robots():
    fetch = FakeFetch({"https://acme.com/sitemap.xml": URLSET})
    lookup = SitemapCareersFinder(fetch).find("acme.com")
    assert lookup.careers_url == "https://acme.com/careers"
    assert lookup.source == "root_sitemap"
    assert fetch.seen == ["https://acme.com/robots.txt", "https://acme.com/sitemap.xml"]


def test_finder_none_without_sitemaps():
    fetch = FakeFetch({})
    lookup = SitemapCareersFinder(fetch).find("acme.com")
    assert lookup.careers_url is None
    assert lookup.source == "none"
    assert lookup.requests == 2  # robots.txt + the /sitemap.xml fallback


def test_finder_respects_max_requests():
    fetch = FakeFetch(
        {
            "https://acme.com/robots.txt": "Sitemap: https://acme.com/sitemap.xml\n",
            "https://acme.com/sitemap.xml": INDEX,
        }
    )
    lookup = SitemapCareersFinder(fetch, max_requests=2).find("acme.com")
    assert len(fetch.seen) == 2
    assert lookup.careers_url is None


def test_finder_skips_gzipped_children():
    fetch = FakeFetch(
        {
            "https://acme.com/robots.txt": "Sitemap: https://acme.com/sitemap.xml\n",
            "https://acme.com/sitemap.xml": (
                "<sitemapindex>"
                "<sitemap><loc>https://acme.com/sm.xml.gz</loc></sitemap>"
                "<sitemap><loc>https://acme.com/sitemap-jobs.xml</loc></sitemap>"
                "</sitemapindex>"
            ),
            "https://acme.com/sitemap-jobs.xml": URLSET,
        }
    )
    lookup = SitemapCareersFinder(fetch).find("acme.com")
    assert lookup.careers_url == "https://acme.com/careers"
    assert "https://acme.com/sm.xml.gz" not in fetch.seen


def test_parse_sitemap_caps_loc_entries():
    from src.identity.sitemap_careers import MAX_LOCS_PER_SITEMAP

    body = "<urlset>" + "".join(
        f"<url><loc>https://acme.com/p/{i}</loc></url>" for i in range(MAX_LOCS_PER_SITEMAP + 500)
    ) + "</urlset>"
    doc = parse_sitemap(body)
    assert doc.kind == "urlset"
    assert len(doc.urls) == MAX_LOCS_PER_SITEMAP


def test_finder_prioritises_page_sitemaps_over_taxonomies():
    fetch = FakeFetch(
        {
            "https://acme.com/robots.txt": "Sitemap: https://acme.com/sitemap_index.xml\n",
            "https://acme.com/sitemap_index.xml": (
                "<sitemapindex>"
                "<sitemap><loc>https://acme.com/category-sitemap.xml</loc></sitemap>"
                "<sitemap><loc>https://acme.com/ai_101-sitemap.xml</loc></sitemap>"
                "<sitemap><loc>https://acme.com/topic-sitemap.xml</loc></sitemap>"
                "<sitemap><loc>https://acme.com/page-sitemap.xml</loc></sitemap>"
                "</sitemapindex>"
            ),
            "https://acme.com/page-sitemap.xml": (
                "<urlset><url><loc>https://acme.com/careers/</loc></url></urlset>"
            ),
        }
    )
    lookup = SitemapCareersFinder(fetch).find("acme.com")
    assert lookup.careers_url == "https://acme.com/careers/"
    assert "https://acme.com/page-sitemap.xml" in fetch.seen
    # With DEFAULT_MAX_SITEMAPS=3 and four children the budget still reaches the
    # first taxonomy child; the guarantee is that the page sitemap comes first
    # (the old alphabetical ordering fetched ai_101/category and missed it), and
    # that the lowest-ranked taxonomy child is dropped.
    assert fetch.seen.index("https://acme.com/page-sitemap.xml") < fetch.seen.index(
        "https://acme.com/category-sitemap.xml"
    )
    assert "https://acme.com/topic-sitemap.xml" not in fetch.seen
