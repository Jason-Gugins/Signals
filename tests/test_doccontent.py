"""Task 1 (extraction-before-claims): format sniff + envelope human-content extractors.

Synthetic fixtures mirror the MEASURED envelope shapes (plan grounding,
2026-10-29): greenhouse ATS JSON (jobs list with title/location/company_name/
updated_at and NO description key), iTunes Search JSON (results with
artistName/description/trackName, shipped as text/javascript), sitemap XML
that ships under an rss+xml content type (the measured body-sniff lesson),
RSS with HTML anchor-soup item descriptions, robots.txt, html, plain text.
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


def test_human_text_robots_html_text_pass_through_unchanged():
    for ct, body, kind in (
        ("text/plain", ROBOTS, "robots"),
        ("text/html", HTML, "html"),
        ("text/plain", PLAIN, "text"),
    ):
        info = human_text(ct, body, publisher_hint=None)
        assert info["kind"] == kind
        assert info["text"] == body  # byte-identical, no html_to_text at this seam
        assert info["excluded"] is False
        assert info["reason"] is None
        assert info["wrong_entity_dropped"] == 0
    # explicit: html markup is NOT converted
    assert "<b>world</b>" in human_text("text/html", HTML, publisher_hint=None)["text"]


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
    """Real-byte smoke over the measured databricks greenhouse envelope."""
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
