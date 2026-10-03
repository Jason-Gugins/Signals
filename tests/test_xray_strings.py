# tests/test_xray_strings.py
"""PURE builder: operators, slot filling, deterministic order, engine URLs."""
import pytest

from src.sources.xray.strings import build_query, encode_query, quote_phrase

SPEC = {"id": "people_title_city", "kind": "people", "site": "linkedin.com/in",
        "variants": ["{title}"], "phrases": ["{location}"],
        "exclude": ["jobs", "preferred"]}

def test_full_people_query():
    q = build_query(SPEC, title="head of growth", location="Austin")
    assert q == 'site:linkedin.com/in "head of growth" "Austin" -"jobs" -"preferred"'

def test_or_group_for_variants():
    spec = {**SPEC, "variants": ["vp sales", "head of sales"], "phrases": [], "exclude": []}
    q = build_query(spec, title=None)
    assert q == 'site:linkedin.com/in ("vp sales" OR "head of sales")'

def test_unfilled_slot_drops_phrase():
    q = build_query(SPEC, title="head of growth", location=None)
    assert '"Austin"' not in q and "{location}" not in q

def test_deterministic_operator_order():
    spec = {"id": "x", "kind": "intent", "site": "linkedin.com/jobs",
            "intitle": "hiring", "inurl": "jobs", "variants": ["a", "b"],
            "phrases": ["c"], "exclude": ["d"]}
    q = build_query(spec)
    assert q == ('site:linkedin.com/jobs intitle:"hiring" inurl:jobs '
                 '("a" OR "b") "c" -"d"')

def test_quote_phrase_strips_inner_quotes_and_collapses_ws():
    assert quote_phrase('  head  of  growth ') == '"head of growth"'

def test_encode_query_google_and_ddg():
    url = encode_query('"vp sales" OR "head of sales"', "google")
    assert url.startswith("https://www.google.com/search?q=") and "num=20" in url
    url = encode_query('"vp sales"', "ddg")
    assert url.startswith("https://html.duckduckgo.com/html/?q=")

def test_encode_query_ddg_lite():
    # The ONLY probe-validated engine path (data/probe/XRAY_SERP_2026_10.md).
    url = encode_query('site:linkedin.com/in "head of growth"', "ddg_lite")
    assert url.startswith("https://lite.duckduckgo.com/lite/?q=")
    assert "site%3Alinkedin.com%2Fin" in url

def test_encode_query_unknown_engine_raises():
    with pytest.raises(ValueError):
        encode_query("x", "bing")
