"""Task 1 (extraction-before-claims): format sniff + envelope human-content extractors.

Synthetic fixtures mirror the MEASURED envelope shapes (plan grounding,
2026-10-29): greenhouse ATS JSON (jobs list with title/location/company_name/
updated_at and NO description key), iTunes Search JSON (results with
artistName/description/trackName, shipped as text/javascript), sitemap XML
that ships under an rss+xml content type (the measured body-sniff lesson),
RSS with HTML anchor-soup item descriptions, robots.txt, html, plain text —
plus the sourcing-spike arm A shape (2026-10-30): jobs whose `content` is
HTML-escaped HTML, prose stripped and capped per job into the extraction —
generalized (ATS-description-aliases, 2026-10-30) to the description alias
set content | descriptionPlain | descriptionHtml | description, with the
measured Ashby shape (no company_name, plain descriptionPlain, raw
descriptionHtml) as a synthetic fixture and a stored rillet envelope smoke —
plus the html extraction branch (task 2): Webflow-shaped sitecontent pages
and the ats_discovery careers page extract to prose at the seam instead of
passing through as markup.
"""
from __future__ import annotations

import gzip
import json
import sqlite3
from pathlib import Path

import pytest

from src.core.doccontent import human_text, publisher_matches, sniff

REPO = Path(__file__).resolve().parents[1]

# --- fixtures: measured envelope shapes ---------------------------------

GREENHOUSE_150 = json.dumps(
    {
        "jobs": [
            {
                "absolute_url": f"https://boards.greenhouse.io/databricks/jobs/{1000 + i}",
                "company_name": "Zibma Infotech" if i == 149 else "Databricks Inc.",
                "first_published": f"2026-0{(i // 28) + 1}-{(i % 28) + 1:02d}T09:00:00Z",
                "id": 1000 + i,
                "location": f"City {i:03d}",
                "metadata": [{"name": "gdpr", "value": "include"}],
                "title": f"Engineer {i:03d}",
                "updated_at": f"2026-0{(i // 28) + 1}-{(i % 28) + 1:02d}T12:00:00Z",
            }
            for i in range(150)
        ]
    }
)
# strictly increasing updated_at with i ⇒ newest-100 window is i in 50..149;
# the wrong-entity job (Zibma, i=149) sits inside the window.

ITUNES = json.dumps(
    {
        "resultCount": 3,
        "results": [
            {
                "artistName": "Databricks Inc.",
                "trackName": "Databricks Learning",
                "description": "Learn data engineering with lessons and quizzes.",
            },
            {
                "artistName": "Zibma Infotech",
                "trackName": "Rillet Education",
                "description": "Education app for finance teams.",
            },
            {
                "trackName": "Mystery App",
                "description": "No artist listed here.",
            },
        ],
    }
)

RSS = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
    "<channel><title>Co Blog</title>"
    "<item><title>Launch notes</title>"
    "<description>&lt;p&gt;Read the &lt;a href=\"https://news.google.com/redirect?x=1\"&gt;"
    "full story&lt;/a&gt; about Agents.&lt;/p&gt;</description>"
    "</item>"
    "<item><title>Second post</title>"
    "<content:encoded><![CDATA[<a href=\"https://example.com/x\">Plain anchor text</a>]]>"
    "</content:encoded></item>"
    "</channel></rss>"
)

ATOM = (
    '<feed xmlns="http://www.w3.org/2005/Atom"><title>AF</title>'
    "<entry><title>Entry One</title><summary>Summary prose here.</summary></entry></feed>"
)

RSS_ZERO_ITEMS = '<rss version="2.0"><channel><title>Empty</title></channel></rss>'

SITEMAP = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    "<url><loc>https://rillet.example.com/a</loc></url>"
    "<url><loc>https://rillet.example.com/b</loc></url></urlset>"
)

ROBOTS = (
    "User-agent: *\nDisallow: /private/\n"
    "Sitemap: https://rillet.example.com/sitemap.xml"
)

HTML = "<html><body><p>Hello <b>world</b></p></body></html>"

PLAIN = "Quarterly revenue grew 40% on the back of enterprise expansion."


# --- sniff ---------------------------------------------------------------


def test_sniff_body_wins_over_content_type():
    # The measured lesson: the sitemap ships as application/rss+xml but the
    # body is <urlset> — body-sniff must win.
    assert sniff("application/rss+xml", SITEMAP) == "sitemap"
    assert sniff("application/json", GREENHOUSE_150) == "json_jobs"
    # measured: the iTunes envelope ships as text/javascript
    assert sniff("text/javascript", ITUNES) == "json_results"
    assert sniff("application/rss+xml", RSS) == "rss"
    assert sniff("application/xml", ATOM) == "rss"
    assert sniff("text/plain", ROBOTS) == "robots"
    assert sniff("text/html", HTML) == "html"
    assert sniff("text/plain", PLAIN) == "text"


def test_sniff_content_type_is_only_a_tiebreaker():
    # A markerless body under an html content type promotes to html (cosmetic:
    # html/text pass through identically), but a markerless body under an rss
    # content type must NOT become rss — body-sniff first, no header override.
    assert sniff("text/html", "plain words only") == "html"
    assert sniff("application/rss+xml", "plain words only") == "text"
    # malformed JSON never sniffs as a json kind
    assert sniff("application/json", '{"jobs": [') == "text"
    assert sniff("application/json", "[1, 2, 3]") == "text"
    # json envelope shapes are exact: results without artistName is not json_results
    assert sniff("application/json", json.dumps({"results": [{"trackName": "x"}]})) == "text"
    assert sniff("application/json", json.dumps({"jobs": "not-a-list"})) == "text"


# --- human_text: json_jobs ------------------------------------------------


def test_human_text_json_jobs_extraction_and_wrong_entity_drop():
    info = human_text("application/json", GREENHOUSE_150, publisher_hint="Databricks")
    assert info["kind"] == "json_jobs"
    assert info["excluded"] is False
    assert info["reason"] is None
    lines = info["text"].splitlines()
    # newest-100 window (i 50..149), minus the one wrong-entity job (i=149)
    assert len(lines) == 99
    assert info["publisher_items"] == 100
    assert info["wrong_entity_dropped"] == 1
    # clean labeled line, exact measured format; no scaffolding keys leak
    assert "Engineer 050 — City 050 — Databricks Inc." in lines
    assert not any("absolute_url" in ln or "metadata" in ln or "gdpr" in ln for ln in lines)
    assert not any("https://" in ln for ln in lines)
    # the Zibma job's content is gone
    assert not any("Engineer 149" in ln or "Zibma" in ln for ln in lines)
    # window cut: i=49 (oldest outside the newest 100) is absent
    assert not any("Engineer 049" in ln for ln in lines)


def test_human_text_json_jobs_without_hint_keeps_everything():
    info = human_text("application/json", GREENHOUSE_150, publisher_hint=None)
    assert info["kind"] == "json_jobs"
    assert info["wrong_entity_dropped"] == 0
    assert len(info["text"].splitlines()) == 100
    assert "Engineer 149 — City 149 — Zibma Infotech" in info["text"]


def test_human_text_json_jobs_recency_key_fallback_and_file_order():
    # no updated_at ⇒ order by first_published desc
    body = json.dumps(
        {
            "jobs": [
                {"title": "A", "location": "L1", "company_name": "X", "first_published": "2026-01-02"},
                {"title": "B", "location": "L2", "company_name": "X", "first_published": "2026-03-01"},
                {"title": "C", "location": "L3", "company_name": "X", "first_published": "2026-02-01"},
            ]
        }
    )
    info = human_text("application/json", body, publisher_hint="X")
    assert [ln.split(" ")[0] for ln in info["text"].splitlines()] == ["B", "C", "A"]
    # neither key ⇒ file order preserved
    body2 = json.dumps(
        {
            "jobs": [
                {"title": "P", "location": "L", "company_name": "X"},
                {"title": "Q", "location": "L", "company_name": "X"},
                {"title": "R", "location": "L", "company_name": "X"},
            ]
        }
    )
    info2 = human_text("application/json", body2, publisher_hint="X")
    assert [ln.split(" ")[0] for ln in info2["text"].splitlines()] == ["P", "Q", "R"]


def test_human_text_json_jobs_missing_company_is_unjudged_not_wrong():
    body = json.dumps(
        {
            "jobs": [
                {"title": "NoCo", "location": "L1"},
                {"title": "Wrong", "location": "L2", "company_name": "Zibma Infotech"},
            ]
        }
    )
    info = human_text("application/json", body, publisher_hint="Rillet")
    assert info["wrong_entity_dropped"] == 1
    assert info["unjudged"] == 1  # can't judge ≠ wrong: kept, counted
    assert info["publisher_items"] == 1
    assert "NoCo — L1" in info["text"]
    assert "Wrong" not in info["text"]


def test_human_text_json_jobs_skips_non_dict_entries_without_raising():
    body = json.dumps(
        {
            "jobs": [
                {"title": "A", "location": "L", "company_name": "Databricks"},
                "junk",
                5,
                None,
            ]
        }
    )
    info = human_text("application/json", body, publisher_hint="Databricks")
    assert info["text"] == "A — L — Databricks"


# --- json_jobs content branch (sourcing spike arm A, 2026-10-30) -----------
# Measured wire shape (boards-api.greenhouse.io …/jobs?content=true, spike
# SOURCING_SPIKE_20261030.md): each job's `content` is HTML-escaped HTML
# ("&lt;p&gt;…") — ONE html.unescape restores the markup, html_to_text
# strips it (the same shape parse_greenhouse strips).

_GREENHOUSE_CONTENT_RAW = (
    "&lt;p&gt;Req ID: CSQ427R266&lt;br&gt;Location: Tokyo, Japan&lt;/p&gt;\n"
    "&lt;p&gt;You will build lakehouse &amp;amp; AI platforms for customers.&lt;/p&gt;\n"
    "&lt;script&gt;tracker()&lt;/script&gt;"
)
_GREENHOUSE_CONTENT_PROSE = (
    "Req ID: CSQ427R266\nLocation: Tokyo, Japan\n"
    "You will build lakehouse & AI platforms for customers."
)

GREENHOUSE_CONTENT = json.dumps(
    {
        "jobs": [
            {
                "id": 1,
                "title": "Solutions Architect",
                "location": {"name": "Tokyo, Japan"},  # measured: dict shape
                "company_name": "Databricks Inc.",
                "updated_at": "2026-10-30T12:00:00Z",
                "content": _GREENHOUSE_CONTENT_RAW,
            },
            {
                "id": 2,
                "title": "Bare Role",
                "location": "Berlin, Germany",
                "company_name": "Databricks Inc.",
                "updated_at": "2026-10-30T11:00:00Z",
                # no content: metadata-only line, exactly as shipped
            },
        ]
    }
)


def test_human_text_json_jobs_content_prose_flows_through():
    info = human_text("application/json", GREENHOUSE_CONTENT, publisher_hint="Databricks")
    assert info["kind"] == "json_jobs"
    assert info["excluded"] is False
    lines = info["text"].splitlines()
    # metadata + 3 prose lines for job 1, metadata line for job 2
    assert len(lines) == 5
    # the content-bearing job's line: metadata line, then stripped prose
    assert lines[0] == "Solutions Architect — Tokyo, Japan — Databricks Inc."
    assert lines[1:4] == _GREENHOUSE_CONTENT_PROSE.splitlines()
    assert lines[4] == "Bare Role — Berlin, Germany — Databricks Inc."
    # no escaped-entity or markup residue from the measured double-escaped form
    assert "&lt;" not in info["text"] and "&gt;" not in info["text"]
    assert "<p>" not in info["text"] and "<script" not in info["text"]
    # publisher counters unchanged: window jobs carrying company_name
    assert info["publisher_items"] == 2
    assert info["wrong_entity_dropped"] == 0
    assert info["unjudged"] == 0
    # the contentless job keeps its shipped metadata-only line
    assert "Bare Role — Berlin, Germany — Databricks Inc." in info["text"]


def test_human_text_json_jobs_content_capped_per_job():
    long_html = "&lt;p&gt;" + ("word " * 20_000) + "&lt;/p&gt;"
    body = json.dumps(
        {
            "jobs": [
                {
                    "title": "Big",
                    "location": "L",
                    "company_name": "Databricks",
                    "updated_at": "2026-10-30T12:00:00Z",
                    "content": long_html,
                }
            ]
        }
    )
    from src.core.doccontent import JOB_CONTENT_CHARS

    info = human_text("application/json", body, publisher_hint="Databricks")
    lines = info["text"].splitlines()
    assert lines[0] == "Big — L — Databricks"
    prose = "\n".join(lines[1:])
    assert len(prose) == JOB_CONTENT_CHARS == 2000
    assert info["text"].count("word") == 400  # 2000 chars / 5 per "word "
    # doc-level cap stays the backstop
    assert len(info["text"]) <= 120_000


def test_human_text_json_jobs_wrong_entity_content_dropped():
    body = json.dumps(
        {
            "jobs": [
                {
                    "title": "Not Ours",
                    "location": "Elsewhere",
                    "company_name": "Zibma Infotech",
                    "updated_at": "2026-10-30T12:00:00Z",
                    "content": _GREENHOUSE_CONTENT_RAW,
                }
            ]
        }
    )
    info = human_text("application/json", body, publisher_hint="Databricks")
    # the wrong-entity job's PROSE is excluded with it
    assert info["wrong_entity_dropped"] == 1
    assert info["publisher_items"] == 1
    assert "Req ID" not in info["text"]
    assert "Not Ours" not in info["text"]
    assert info["text"] == ""


def test_human_text_json_jobs_unjudged_content_kept():
    body = json.dumps(
        {
            "jobs": [
                {
                    "title": "NoCo",
                    "location": "L1",
                    "updated_at": "2026-10-30T12:00:00Z",
                    "content": _GREENHOUSE_CONTENT_RAW,
                }
            ]
        }
    )
    info = human_text("application/json", body, publisher_hint="Rillet")
    # no company_name ⇒ can't judge ≠ wrong: kept, counted unjudged, prose intact
    assert info["unjudged"] == 1
    assert info["wrong_entity_dropped"] == 0
    assert info["text"].splitlines()[0] == "NoCo — L1"
    assert "Req ID: CSQ427R266" in info["text"]


# --- json_jobs description alias set (ATS-description-aliases, 2026-10-30) --
# Measured Ashby shape (rillet ats_ashby envelope, doc 1ed1012f…): job keys =
# address/applyUrl/compensation/department/descriptionHtml/descriptionPlain/
# employmentType/id/isListed/isRemote/jobUrl/location/publishedAt/
# secondaryLocations/shouldDisplayCompensationOnJobPostings/team/title/
# workplaceType — NO company_name key anywhere (organization None).
# descriptionPlain is clean plain text; descriptionHtml is raw HTML. The
# prose source is the FIRST non-empty of content | descriptionPlain |
# descriptionHtml | description (greenhouse `content` keeps priority), all
# treated uniformly: html_to_text(unescape(...))[:JOB_CONTENT_CHARS].

_ASHBY_PLAIN = (
    "WHAT WE DO\n\nRillet serves accounting and finance teams with close automation."
)
_ASHBY_HTML = (
    "<style>.hero{color:red}</style>"
    "<h1>What We Do</h1>"
    '<p style="min-height:1.5em">Rillet builds the close automation platform.</p>'
    "<script>tracker()</script>"
)


def _ashby_job(**overrides):
    job = {
        "id": "8f14e45f",
        "title": "Senior Accountant",
        "location": "San Francisco",
        "isRemote": True,
        "organization": None,  # measured: no company_name key in the envelope
        "employmentType": "FULL_TIME",
        "publishedAt": "2026-10-28T12:00:00.000Z",
        "jobUrl": "https://jobs.ashbyhq.com/rillet/senior-accountant",
        "applyUrl": "https://jobs.ashbyhq.com/rillet/senior-accountant/application",
        "secondaryLocations": [],
    }
    job.update(overrides)
    return job


ASHBY_SHAPE = json.dumps(
    {
        "jobs": [
            _ashby_job(title="Senior Accountant", location="San Francisco",
                       descriptionPlain=_ASHBY_PLAIN),
            _ashby_job(title="Platform Engineer", location="Remote",
                       descriptionPlain="", descriptionHtml=_ASHBY_HTML),
        ]
    }
)


def test_human_text_json_jobs_ashby_alias_shape():
    info = human_text("application/json", ASHBY_SHAPE, publisher_hint="Rillet")
    assert info["kind"] == "json_jobs"
    assert info["excluded"] is False
    text = info["text"]
    # descriptionPlain prose survives (plain text, whitespace-collapsed per line)
    assert "WHAT WE DO" in text
    assert "Rillet serves accounting and finance teams" in text
    # descriptionHtml's tags/styles/scripts do NOT survive
    assert "<h1>" not in text
    assert "<p" not in text
    assert ".hero" not in text  # <style> body dropped
    assert "tracker()" not in text  # <script> body dropped
    assert "min-height" not in text  # attribute soup dropped
    # the Html-sourced job's prose survives stripped
    assert "Rillet builds the close automation platform." in text
    # no company_name ⇒ lines stay clean, no "None" anywhere
    assert "None" not in text
    assert "Senior Accountant — San Francisco" in text
    assert "Platform Engineer — Remote" in text
    # no company_name in the envelope ⇒ unjudged, never wrong
    assert info["publisher_items"] == 0
    assert info["unjudged"] == 2
    assert info["wrong_entity_dropped"] == 0


def test_human_text_json_jobs_alias_priority_content_wins():
    # greenhouse shape unchanged: `content` keeps priority over descriptionPlain
    body = json.dumps(
        {
            "jobs": [
                {
                    "title": "Solutions Architect",
                    "location": "Tokyo, Japan",
                    "company_name": "Databricks Inc.",
                    "updated_at": "2026-10-30T12:00:00Z",
                    "content": "&lt;p&gt;Greenhouse content prose marker.&lt;/p&gt;",
                    "descriptionPlain": "Ashby plain prose marker.",
                }
            ]
        }
    )
    info = human_text("application/json", body, publisher_hint="Databricks")
    assert "Greenhouse content prose marker." in info["text"]
    assert "Ashby plain prose marker." not in info["text"]


def test_human_text_json_jobs_description_plain_idempotent():
    # a markup-free descriptionPlain extracts unmangled: html_to_text is
    # idempotent on plain text — only the whitespace collapse applies
    plain = "Responsible for month-end close.\n\nWorks with the finance team on P&L reviews."
    body = json.dumps(
        {"jobs": [{"title": "Staff Accountant", "location": "Boston", "descriptionPlain": plain}]}
    )
    info = human_text("application/json", body, publisher_hint="Rillet")
    assert info["text"] == (
        "Staff Accountant — Boston\n"
        "Responsible for month-end close.\n"
        "Works with the finance team on P&L reviews."
    )


def test_human_text_json_jobs_description_plain_capped_per_job():
    from src.core.doccontent import JOB_CONTENT_CHARS

    body = json.dumps(
        {"jobs": [{"title": "Big", "location": "L", "descriptionPlain": "a" * 5_000}]}
    )
    info = human_text("application/json", body, publisher_hint="Rillet")
    lines = info["text"].splitlines()
    assert lines[0] == "Big — L"
    prose = "\n".join(lines[1:])
    assert len(prose) == JOB_CONTENT_CHARS == 2000
    # doc-level cap stays the backstop
    assert len(info["text"]) <= 120_000


# --- human_text: json_results ----------------------------------------------


def test_human_text_json_results_per_result_matching():
    info = human_text("text/javascript", ITUNES, publisher_hint="Databricks")
    assert info["kind"] == "json_results"
    assert info["excluded"] is False
    assert info["publisher_items"] == 2  # two results carry artistName
    assert info["wrong_entity_dropped"] == 1  # Zibma Infotech ≠ Databricks
    assert info["unjudged"] == 1  # missing artistName ⇒ kept + counted
    text = info["text"]
    assert "Databricks Learning: Learn data engineering with lessons and quizzes." in text
    assert "Mystery App: No artist listed here." in text
    assert "Rillet Education" not in text
    assert "Education app for finance teams." not in text


def test_human_text_json_results_without_hint_judges_nothing():
    info = human_text("text/javascript", ITUNES, publisher_hint=None)
    assert info["wrong_entity_dropped"] == 0
    assert info["unjudged"] == 1
    assert info["publisher_items"] == 2
    assert "Education app for finance teams." in info["text"]


# --- human_text: rss --------------------------------------------------------


def test_human_text_rss_strips_item_description_html_to_anchor_text():
    info = human_text("application/rss+xml", RSS, publisher_hint="Databricks")
    assert info["kind"] == "rss"
    assert info["excluded"] is False
    text = info["text"]
    assert "Launch notes: Read the full story about Agents." in text
    # anchor soup stripped to anchor text: no URLs, no markup survives
    assert "news.google.com" not in text
    assert "<a " not in text and "<p>" not in text
    # content:encoded (namespaced) handled via localname
    assert "Second post: Plain anchor text" in text


def test_human_text_rss_atom_entries():
    info = human_text("application/xml", ATOM, publisher_hint=None)
    assert info["kind"] == "rss"
    assert "Entry One: Summary prose here." in info["text"]


def test_human_text_rss_zero_items_falls_back_to_text_passthrough():
    info = human_text("application/rss+xml", RSS_ZERO_ITEMS, publisher_hint=None)
    assert info["kind"] == "rss"
    assert info["text"] == RSS_ZERO_ITEMS  # never silently empty
    assert info["excluded"] is False


# --- human_text: sitemap / robots / html / text -----------------------------


def test_human_text_sitemap_excluded_as_catalog():
    info = human_text("application/rss+xml", SITEMAP, publisher_hint="Rillet")
    assert info == {
        "kind": "sitemap",
        "text": "",
        "publisher_items": 0,
        "excluded": True,
        "reason": "catalog_url_list",
        "wrong_entity_dropped": 0,
        "unjudged": 0,
    }


def test_human_text_robots_text_pass_through_unchanged():
    # robots and text stay byte-passthrough. html does NOT (ATS-description-
    # aliases task 2, 2026-10-30): the old pin here held html byte-identical —
    # that pinned the wart this task fixes. The same HTML fixture now extracts
    # to prose (pinned below; full-scale cases in the html-branch section).
    for ct, body, kind in (
        ("text/plain", ROBOTS, "robots"),
        ("text/plain", PLAIN, "text"),
    ):
        info = human_text(ct, body, publisher_hint=None)
        assert info["kind"] == kind
        assert info["text"] == body  # byte-identical passthrough
        assert info["excluded"] is False
        assert info["reason"] is None
        assert info["wrong_entity_dropped"] == 0
    # html extracts instead of passing through
    info = human_text("text/html", HTML, publisher_hint=None)
    assert info["kind"] == "html"
    assert info["text"] == "Hello world"
    assert "<b>world</b>" not in info["text"]


# --- human_text: html extraction branch (ATS-description-aliases task 2) ----
# Why: stored html docs — sitecontent pages, company_feed posts, wayback-html
# captures, the ats_discovery careers page — reach the decide-layer gates as
# prose, not markup soup (the sourcing wave's sitecontent pages reached the
# gate as Webflow head/CSS noise). Disclosed drift: 13 html docs re-head on
# rillet, 8 on databricks; the dual-hash join (c3abacb) handles tooling.


def _webflow_page() -> str:
    """Webflow-shaped sitecontent page (~15KB): a head carrying <style>/
    <script>, nav boilerplate, and the real case-study prose mid-page."""
    style = ".hero-grid{display:grid;gap:1rem}\n" + "".join(
        f".w-nav-item-{i}{{color:#333;padding:4px {i}px}}\n" for i in range(120)
    )
    script = 'window.trackConversion("pixel-1");\n' + "".join(
        f'analytics.beacon("view", {{slot: {i}}});\n' for i in range(120)
    )
    nav = "".join(f'<a href="/solutions/s{i}">Solution {i}</a>' for i in range(40))
    sections = "".join(
        f"<section><h2>Story {i}</h2>"
        f"<p>Customer {i} deployed the platform in weeks.</p></section>"
        for i in range(80)
    )
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"/>'
        "<title>Customer Stories — Example Co</title>"
        f"<style>{style}</style><script>{script}</script></head><body>"
        f"<nav>{nav}</nav>"
        "<main><h1>Customer stories</h1>"
        "<p>Sitecontent prose marker: Example Co cut month-end close from "
        "nine days to two with automated reconciliation across 40 entities."
        "</p></main>"
        f"{sections}</body></html>"
    )


def test_human_text_html_webflow_body_extracts_prose():
    body = _webflow_page()
    assert len(body) >= 10_000  # page-scale fixture, not a toy
    info = human_text("text/html", body, publisher_hint="Example Co")
    assert info["kind"] == "html"
    assert info["excluded"] is False
    text = info["text"]
    # the mid-page prose reaches the gate
    assert "Sitecontent prose marker" in text
    assert "cut month-end close from nine days to two" in text
    # script/style content does NOT survive
    assert "trackConversion" not in text
    assert "analytics.beacon" not in text
    assert "hero-grid" not in text
    assert "w-nav-item" not in text
    # no tag fragments, no attribute soup
    assert "<" not in text
    assert 'href="/solutions/' not in text
    # extraction shrinks the markup
    assert len(text) < len(body)


def _ats_discovery_page() -> str:
    """ats_discovery-shaped careers page (~400KB, half-scale of the measured
    861,886-byte databricks doc): meta-laden head with kilobytes of CSS/JS,
    filler sections, and the careers prose deep in the body — deep enough
    that the 120,000-char cap binds on the EXTRACTED text while the prose
    (earlier in the extraction order) survives."""
    head = (
        '<!DOCTYPE html><html lang="en-US"><head><meta charSet="utf-8"/>'
        + "".join(f'<meta name="meta-{i}" content="value-{i}"/>' for i in range(200))
        + "<style>"
        + "".join(f".c{i}{{color:#a1b2c3}}\n" for i in range(2000))
        + "</style><script>"
        + "".join(f'track{i}("evt");\n' for i in range(2000))
        + "</script></head><body>"
    )
    filler_before = "".join(
        f"<div>Division {i} field summary line with several filler words.</div>"
        for i in range(1000)
    )
    prose = (
        "<main><h1>Build the platform</h1>"
        "<p>Atsdiscovery prose marker: we build the lakehouse platform for "
        "data intelligence teams worldwide.</p>"
        "<p>Open roles across engineering, sales, and field teams.</p></main>"
    )
    filler_after = "".join(
        f"<div>Department {i} overview paragraph with several words of body text.</div>"
        for i in range(3500)
    )
    return head + filler_before + prose + filler_after + "</body></html>"


def test_human_text_html_ats_discovery_shape_bounded_and_prose():
    body = _ats_discovery_page()
    assert len(body) >= 300_000  # genuinely page-scale (~400KB)
    info = human_text("text/html; charset=utf-8", body, publisher_hint="Databricks")
    assert info["kind"] == "html"
    text = info["text"]
    # bounded by the per-kind cap — and the cap actually binds here
    assert len(text) == 120_000
    # prose deep in the body survives (it precedes the cap cut)
    assert "Atsdiscovery prose marker" in text
    assert "lakehouse platform" in text
    # markup does not survive
    assert "<script" not in text
    assert "<html" not in text
    assert "<div" not in text


# --- malformed / never-raise -------------------------------------------------


def test_human_text_malformed_json_falls_back_to_text():
    raw = '{"jobs": ['
    info = human_text("application/json", raw, publisher_hint="Databricks")
    assert info["kind"] == "text"
    assert info["text"] == raw
    assert info["excluded"] is False
    # truncated xml that sniffs rss but fails to parse ⇒ passthrough too
    raw_xml = "<rss version=\"2.0\"><channel><item><title>T"
    info2 = human_text("application/rss+xml", raw_xml, publisher_hint=None)
    assert info2["kind"] == "rss"
    assert info2["text"] == raw_xml


def test_human_text_never_raises_on_nasty_bodies():
    nasty = [
        "",
        "\ufeff" + RSS,
        "{'jobs': single-quoted}",
        json.dumps({"jobs": [{"title": {"nested": "dict"}, "location": ["list"]}]}),
        "<html <rss <urlset User-agent: Disallow " * 3,
        "<feed><entry><title>",
        "User-agent: bot\nDisallow: /x",
        json.dumps({"jobs": None, "results": None}),
    ]
    for body in nasty:
        info = human_text("application/xml", body, publisher_hint="X")
        assert isinstance(info, dict)
        assert "kind" in info and "text" in info


# --- 120k cap ----------------------------------------------------------------


def test_extracted_text_capped_at_120k_chars():
    info = human_text("text/plain", "x" * 130_000, publisher_hint=None)
    assert len(info["text"]) == 120_000
    results = json.dumps(
        {
            "results": [
                {"artistName": "Rillet", "trackName": "T", "description": "y" * 130_000}
            ]
        }
    )
    info2 = human_text("text/javascript", results, publisher_hint="Rillet")
    assert len(info2["text"]) == 120_000
    assert info2["text"].startswith("T: ")


# --- publisher_matches --------------------------------------------------------


def test_publisher_matches_containment_rule():
    assert publisher_matches("Databricks Inc.", "Databricks", ()) is True
    assert publisher_matches("Databricks", "Databricks Inc.", ()) is True
    assert publisher_matches("DATABRICKS", "databricks", ()) is True
    # the measured wrong-entity case: no match
    assert publisher_matches("Zibma Infotech", "Rillet", ()) is False
    assert publisher_matches("Rillet", "Zibma Infotech", ()) is False
    # empty/unmatchable sides never match
    assert publisher_matches("", "Rillet", ()) is False
    assert publisher_matches("Rillet", "", ()) is False
    assert publisher_matches(None, "Rillet", ()) is False


def test_publisher_matches_aliases():
    # alias matches the publisher side
    assert publisher_matches("Zibma Infotech", "Rillet", ("zibma",)) is True
    # alias matches the hint side
    assert publisher_matches("Zibma Infotech", "Rillet", ("Rillet AI",)) is True
    # containment applies to aliases too (documented false-positive edge)
    assert publisher_matches("Rillet", "Zibma", ("let",)) is True
    # non-matching aliases stay out of the way
    assert publisher_matches("Zibma Infotech", "Rillet", ("unrelated",)) is False


# --- real-byte smoke (skip-when-absent, offline-safe) -------------------------


def _load_stored_greenhouse():
    db_path = REPO / "data" / "signals.db"
    if not db_path.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = con.execute(
            "SELECT doc_id, path, content_type FROM documents WHERE doc_id LIKE 'd8419437%'"
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    if row is None:
        return None
    doc_id, rel, content_type = row
    path = Path(rel) if rel else REPO / "data" / "raw" / doc_id[:2] / f"{doc_id}.gz"
    if not path.exists():
        return None
    with gzip.open(path, "rb") as fh:
        return (content_type or "application/json", fh.read().decode("utf-8", "replace"))


def test_real_stored_greenhouse_smoke():
    """Real-byte smoke over the measured databricks greenhouse envelope.

    The stored d8419437… doc is the CONTENTLESS discovery-probe envelope
    (boards-api /jobs without content=true, 892 metadata-only jobs — see the
    reconciliation test in test_ats_discovery.py), so the content branch adds
    nothing here: expectations are the shipped metadata-only shape."""
    stored = _load_stored_greenhouse()
    if stored is None:
        pytest.skip("stored greenhouse envelope not present")
    content_type, body = stored
    assert sniff(content_type, body) == "json_jobs"
    info = human_text(content_type, body, publisher_hint="Databricks")
    assert info["kind"] == "json_jobs"
    assert info["excluded"] is False
    assert info["text"]
    assert "absolute_url" not in info["text"]
    assert "gdpr" not in info["text"]
    assert " — " in info["text"]
    assert len(info["text"]) <= 120_000
    if "&lt;p&gt;" not in body:
        # contentless stored envelope: every extracted line is metadata-only
        assert all("&lt;" not in ln for ln in info["text"].splitlines())


def _load_stored_greenhouse_content():
    """Content-bearing collector doc (ats_greenhouse, boards-api ?content=true),
    the spike arm A shape — skip-when-absent, offline-safe like the smoke above."""
    db_path = REPO / "data" / "signals.db"
    if not db_path.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = con.execute(
            "SELECT doc_id, path, content_type FROM documents "
            "WHERE source = 'ats_greenhouse' AND url LIKE '%content=true%' "
            "AND domain = 'databricks.com' "
            "ORDER BY fetched_at DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    if row is None:
        return None
    doc_id, rel, content_type = row
    path = Path(rel) if rel else REPO / "data" / "raw" / doc_id[:2] / f"{doc_id}.gz"
    if not path.exists():
        return None
    with gzip.open(path, "rb") as fh:
        return (content_type or "application/json", fh.read().decode("utf-8", "replace"))


def test_real_stored_greenhouse_content_smoke():
    """Real-byte smoke over the CONTENT-BEARING collector envelope (spike arm
    A measured shape: 903/903 jobs carry HTML-escaped HTML content)."""
    stored = _load_stored_greenhouse_content()
    if stored is None:
        pytest.skip("stored content-bearing greenhouse envelope not present")
    content_type, body = stored
    assert sniff(content_type, body) == "json_jobs"
    info = human_text(content_type, body, publisher_hint="Databricks")
    assert info["kind"] == "json_jobs"
    assert info["excluded"] is False
    assert info["text"]
    # the content branch fired: escaped-entity markup and raw tags are gone
    assert "&lt;p&gt;" not in info["text"]
    assert "<script" not in info["text"]
    # prose survived into the extraction (content-bearing lines exist)
    assert "\n" in info["text"]
    assert " — " in info["text"]
    # per-job caps + the doc-level cap hold on 7.3M chars of board prose
    assert len(info["text"]) <= 120_000


def _load_stored_ashby():
    """Rillet Ashby collector doc (ats_ashby, doc 1ed1012f…) — skip-when-absent,
    offline-safe like the greenhouse smokes above."""
    db_path = REPO / "data" / "signals.db"
    if not db_path.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = con.execute(
            "SELECT doc_id, path, content_type FROM documents "
            "WHERE domain = 'rillet.com' AND source = 'ats_ashby' "
            "ORDER BY fetched_at DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    if row is None:
        return None
    doc_id, rel, content_type = row
    path = Path(rel) if rel else REPO / "data" / "raw" / doc_id[:2] / f"{doc_id}.gz"
    if not path.exists():
        return None
    with gzip.open(path, "rb") as fh:
        return (content_type or "application/json", fh.read().decode("utf-8", "replace"))


def test_real_stored_rillet_ashby_smoke():
    """Real-byte smoke over the stored rillet Ashby envelope (measured:
    40 jobs, 40/40 with descriptionPlain, no company_name). Pre-fix baseline:
    1,662 title-line chars; the alias branch should stage real prose."""
    stored = _load_stored_ashby()
    if stored is None:
        pytest.skip("stored rillet Ashby envelope not present")
    content_type, body = stored
    assert sniff(content_type, body) == "json_jobs"
    info = human_text(content_type, body, publisher_hint="Rillet")
    assert info["kind"] == "json_jobs"
    assert info["excluded"] is False
    # the measured descriptionPlain head reaches the extraction
    assert "Rillet serves accounting and finance teams" in info["text"]
    # prose jumped far past the pre-fix 1,662-char title-line baseline
    assert len(info["text"]) > 10_000
    # doc-level cap holds
    assert len(info["text"]) <= 120_000


def _load_stored_databricks_ats_discovery():
    """Databricks ats_discovery careers page (measured: 861,886 bytes of
    text/html) — skip-when-absent, offline-safe like the smokes above."""
    db_path = REPO / "data" / "signals.db"
    if not db_path.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = con.execute(
            "SELECT doc_id, path, content_type FROM documents "
            "WHERE domain = 'databricks.com' AND source = 'ats_discovery' "
            "ORDER BY fetched_at DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    if row is None:
        return None
    doc_id, rel, content_type = row
    path = Path(rel) if rel else REPO / "data" / "raw" / doc_id[:2] / f"{doc_id}.gz"
    if not path.exists():
        return None
    with gzip.open(path, "rb") as fh:
        return (content_type or "text/html", fh.read().decode("utf-8", "replace"))


def test_real_stored_databricks_ats_discovery_html_smoke():
    """Real-byte smoke over the stored databricks ats_discovery careers page
    (861,886 bytes): the html branch extracts prose — no markup soup."""
    stored = _load_stored_databricks_ats_discovery()
    if stored is None:
        pytest.skip("stored databricks ats_discovery doc not present")
    content_type, body = stored
    assert sniff(content_type, body) == "html"
    assert len(body) > 100_000  # page-scale doc (measured 861,886 bytes)
    info = human_text(content_type, body, publisher_hint="Databricks")
    assert info["kind"] == "html"
    assert info["excluded"] is False
    # the per-kind cap bounds the extraction
    assert len(info["text"]) < 120_000
    # prose extracted: no markup fragments survive
    assert "<html" not in info["text"]
    assert "<script" not in info["text"]
    # prose-bearing: the extraction carries real page words
    assert "Databricks" in info["text"]
