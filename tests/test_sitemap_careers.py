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
