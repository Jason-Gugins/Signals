# tests/test_xray_serp_parse.py
"""X-ray SERP parser: challenge detection, DDG-lite anchor extraction, unwrap.

Fixture-backed (real probe bodies, hand-minimized) per Task 4 as re-scoped by
the Task 3/3b probe verdict: google is NO-GO on every transport, so only
engine="ddg_lite" is parsed — the google JS-gate marker stays in the
detection list so a future engine swap inherits honest classification.
"""
from pathlib import Path

import pytest

from src.sources.xray.serp import ParseError, is_challenge, parse_results

FIX = Path(__file__).parent / "fixtures" / "xray"


def _load(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


# --- challenge detection -------------------------------------------------

def test_challenge_202_detected():
    body = _load("ddg_challenge_202.html")
    marker = is_challenge(body)
    assert marker is not None
    assert isinstance(marker, str)


def test_errorlite_403_detected():
    body = _load("ddg_errorlite_403.html")
    marker = is_challenge(body)
    assert marker is not None
    assert isinstance(marker, str)


def test_clean_lite_serp_not_challenge():
    body = _load("ddg_lite_serp.html")
    assert is_challenge(body) is None


def test_organic_snippet_mentioning_challenge_words_is_not_a_challenge():
    # Wave-1 review regression: bare "challenge"/"anomaly" were removed from
    # the marker list — organic hiring/intent snippets routinely carry those
    # words and a false block verdict burns the query (ParseError + ledger).
    body = SYNTHETIC.replace(
        "snippet for a", "We love a challenge. Anomaly detection is our edge."
    )
    assert is_challenge(body) is None
    results = parse_results(body, engine="ddg_lite")
    assert "We love a challenge." in results[0]["snippet"]


def test_google_js_gate_marker_detected():
    # Documented NO-GO per probe: detection stays so a future engine swap
    # inherits honest classification instead of parsing a 0-anchor shell.
    body = '<html><a href="/httpservice/retry/enablejs?sei=x">enable js</a></html>'
    assert is_challenge(body) is not None


# --- parsing the organic lite SERP ---------------------------------------

def test_parse_lite_results():
    body = _load("ddg_lite_serp.html")
    results = parse_results(body, engine="ddg_lite")
    assert len(results) >= 3, "fixture must parse to >=3 organic results"
    urls = [r["url"] for r in results]
    assert len(urls) == len(set(urls)), "results must be deduped by URL"
    for r in results:
        assert r["url"].lower().startswith(("http://", "https://"))
        assert "duckduckgo.com" not in r["url"]
        assert r["title"].strip()
        assert set(r) == {"url", "title", "snippet"}


def test_parse_lite_unwraps_uddg_redirects():
    body = _load("ddg_lite_serp.html")
    results = parse_results(body, engine="ddg_lite")
    urls = [r["url"] for r in results]
    assert any("linkedin.com/in" in u for u in urls), urls
    # the wrapped hrefs carry percent-encoded targets; nothing may survive
    # still wrapped in a duckduckgo.com/l/?uddg= redirect
    assert not any("uddg=" in u for u in urls)
    assert any(u == "https://ca.linkedin.com/in/austin-grant-2465bb9b" for u in urls)


def test_parse_lite_fills_snippets():
    body = _load("ddg_lite_serp.html")
    results = parse_results(body, engine="ddg_lite")
    assert all(r["snippet"].strip() for r in results), (
        "lite markup ships a result-snippet cell per result; it must be filled"
    )


# --- explicit failures (silent empties poison the ledger) ----------------

def test_garbage_body_raises_parse_error():
    with pytest.raises(ParseError):
        parse_results("<html><body>hello</body></html>", engine="ddg_lite")


def test_empty_body_raises_parse_error():
    with pytest.raises(ParseError):
        parse_results("", engine="ddg_lite")


def test_challenge_202_raises_with_marker():
    body = _load("ddg_challenge_202.html")
    with pytest.raises(ParseError) as excinfo:
        parse_results(body, engine="ddg_lite")
    assert "challenge page served" in str(excinfo.value)
    assert "unfortunately, bots use duckduckgo" in str(excinfo.value)


def test_errorlite_403_raises_with_marker():
    body = _load("ddg_errorlite_403.html")
    with pytest.raises(ParseError) as excinfo:
        parse_results(body, engine="ddg_lite")
    assert "challenge page served" in str(excinfo.value)
    assert "error-lite" in str(excinfo.value)


def test_unknown_engine_raises_value_error():
    body = _load("ddg_lite_serp.html")
    with pytest.raises(ValueError):
        parse_results(body, engine="google")


# --- tolerance rules on a synthetic body (same lite markup shape) --------

SYNTHETIC = """
<table border="0">
  <tr><td>1.&nbsp;</td>
      <td><a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&amp;rut=x" class='result-link'>Example A</a></td></tr>
  <tr><td></td><td class='result-snippet'>snippet for a</td></tr>
  <tr><td>2.&nbsp;</td>
      <td><a rel="nofollow" href="//duckduckgo.com/y.js?ad_provider=foo" class='result-link'>Ad junk</a></td></tr>
  <tr><td>3.&nbsp;</td>
      <td><a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.example.com%2Fa&amp;rut=y" class='result-link'>Example A dup</a></td></tr>
  <tr><td></td><td class='result-snippet'>snippet for dup</td></tr>
  <tr><td>4.&nbsp;</td>
      <td><a rel="nofollow" href="/about" class='result-link'>Internal</a></td></tr>
</table>
"""


def test_synthetic_ad_links_dropped():
    results = parse_results(SYNTHETIC, engine="ddg_lite")
    titles = [r["title"] for r in results]
    assert "Ad junk" not in titles


def test_synthetic_internal_and_relative_links_dropped():
    results = parse_results(SYNTHETIC, engine="ddg_lite")
    assert all("duckduckgo.com" not in r["url"] for r in results)
    assert all(r["url"].lower().startswith(("http://", "https://")) for r in results)


def test_synthetic_dedupe_by_unwrapped_url():
    # https://example.com/a and https://www.example.com/a unwrap to the two
    # distinct hrefs above; the SAME wrapped href twice must dedupe.
    body = SYNTHETIC + """
  <tr><td>5.&nbsp;</td>
      <td><a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&amp;rut=x" class='result-link'>Example A again</a></td></tr>
</table>
"""
    results = parse_results(body, engine="ddg_lite")
    urls = [r["url"] for r in results]
    assert len(urls) == len(set(urls))
    assert urls.count("https://example.com/a") == 1


def test_synthetic_snippet_pairs_with_preceding_anchor():
    results = parse_results(SYNTHETIC, engine="ddg_lite")
    by_url = {r["url"]: r for r in results}
    assert by_url["https://example.com/a"]["snippet"] == "snippet for a"
