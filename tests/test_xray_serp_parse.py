# tests/test_xray_serp_parse.py
"""X-ray SERP parser: challenge detection, DDG-lite anchor extraction, unwrap.

Fixture-backed (real probe bodies, hand-minimized) per Task 4 as re-scoped by
the Task 3/3b probe verdict, extended by the Google bypass ladder Task 6
(dispatch verdicts in data/probe/XRAY_SERP_2026_10.md): engine="google_state"
parses the ``window.W_jd`` embedded "2003" url+title records — the headed
browser / cookie-replay captures carry a full organic result set as state
while every href is an opaque relative /goto?url= blob, so anchors are
useless there. ddg_lite parsing is unchanged.
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


# --- google_state engine (bypass ladder Task 6) ---------------------------
#
# Fixture = tests/fixtures/xray/google_state_serp.html, hand-minimized from
# tmp/probe_xray_google_browser.html (751KB headed capture): 12 real
# "2003" records + one verbatim duplicate + one synthetic google-internal
# record + the standard enablejs noscript block placed beyond the 20k
# challenge-front-load window exactly as on the real body (there: ~141k).

GS_FIXTURE = "google_state_serp.html"

# the fixture carries 12 unique real records; the duplicate collapses and the
# google-internal record drops, so exactly these 12 survive
GS_EXPECTED_FIRST = "https://www.linkedin.com/in/tylerdurman"
GS_EXPECTED_LAST = "https://www.linkedin.com/in/matthewswan15"


def test_parse_google_state_fixture():
    body = _load(GS_FIXTURE)
    results = parse_results(body, engine="google_state")
    assert len(results) >= 6, "fixture must parse to >=6 organic results"
    urls = [r["url"] for r in results]
    assert len(urls) == len(set(urls)), "results must be deduped by URL"
    for r in results:
        assert r["url"].lower().startswith(("http://", "https://"))
        assert set(r) == {"url", "title", "snippet"}
        assert r["title"].strip()
    assert sum("linkedin.com/in" in u for u in urls) >= 2


def test_google_state_fixture_record_count_and_order():
    # 12 real records: duplicate collapsed, google-internal dropped, real
    # document order preserved (fixture built from the capture verbatim).
    results = parse_results(_load(GS_FIXTURE), engine="google_state")
    urls = [r["url"] for r in results]
    assert len(urls) == 12
    assert urls[0] == GS_EXPECTED_FIRST
    assert urls[-1] == GS_EXPECTED_LAST
    assert "https://www.linkedin.com/in/austinheaton" in urls


def test_google_state_drops_google_internal_urls():
    results = parse_results(_load(GS_FIXTURE), engine="google_state")
    assert not any("google.com" in r["url"] for r in results)


def test_google_state_snippet_always_empty():
    # Documented limitation: W_jd records carry url+title only — there is no
    # snippet in the embedded state. hits.py works on titles/URLs.
    results = parse_results(_load(GS_FIXTURE), engine="google_state")
    assert all(r["snippet"] == "" for r in results)


def test_google_state_clean_body_without_wjd_raises():
    # The 93KB cookie-less shell family: a clean body with NO W_jd state at
    # all must NEVER parse as zero results (silent empties poison the ledger).
    body = "<html><head><title>Google Search</title></head><body>hello</body></html>"
    with pytest.raises(ParseError) as excinfo:
        parse_results(body, engine="google_state")
    assert "no result records parsed from body" in str(excinfo.value)


def test_google_state_early_enablejs_with_records_is_soft():
    # Wave-review fix: enablejs appears in the <noscript> of EVERY Google page
    # INCLUDING result-bearing ones (Task 1) — when the body carries W_jd
    # records, an early enablejs marker must NOT classify the body as a
    # challenge (results are record-gated, so this door admits no captcha).
    body = (
        '<html><head><title>Google Search</title></head><body>'
        '<noscript><a href="/httpservice/retry/enablejs?sei=x">enable js</a></noscript>'
        '<script>window.W_jd={"2003":[null,"tok","https://www.linkedin.com/in/janedoe","Jane Doe - Head of Growth"]};'
        "</script></body></html>"
    )
    results = parse_results(body, engine="google_state")
    assert [r["url"] for r in results] == ["https://www.linkedin.com/in/janedoe"]
    assert results[0]["title"] == "Jane Doe - Head of Growth"


def test_google_state_early_enablejs_without_records_still_challenge():
    # The same early enablejs WITHOUT records (the 93KB shell family) still
    # raises as a challenge — the soft-marker door admits nothing else.
    body = (
        '<html><head><title>Google Search</title></head><body>'
        '<noscript><a href="/httpservice/retry/enablejs?sei=x">enable js</a></noscript>'
        "<p>no results here</p></body></html>"
    )
    with pytest.raises(ParseError) as excinfo:
        parse_results(body, engine="google_state")
    assert "challenge page served" in str(excinfo.value)


def test_google_state_wjd_without_records_raises():
    body = (
        "<html><script>var a={};if(window.W_jd)for(var b in a)"
        "window.W_jd[b]=a[b];else window.W_jd=a;</script></html>"
    )
    with pytest.raises(ParseError) as excinfo:
        parse_results(body, engine="google_state")
    assert "no result records parsed from body" in str(excinfo.value)


def test_google_state_captcha_body_raises_with_marker():
    # The live 429 shape (tmp/probe_xray_google_replay_q1.html, ladder Tasks
    # 2+3): unusual-traffic captcha interstitial — must surface as an explicit
    # challenge, never parse as results.
    body = (
        "<html><head><title>https://www.google.com/search?q=x&amp;hl=en</title></head>"
        '<body><div id="recaptcha" class="g-recaptcha" data-sitekey="6Lfw"></div>'
        "Our systems have detected unusual traffic from your computer network."
        "</body></html>"
    )
    with pytest.raises(ParseError) as excinfo:
        parse_results(body, engine="google_state")
    assert "challenge page served" in str(excinfo.value)
    assert "captcha" in str(excinfo.value)


def test_google_state_js_escapes_decoded():
    # Google escapes =, &, ' as \\u003d \\u0026 \\u0027 inside the state
    # strings; the parser must decode them (analyze_google_shell.py approach).
    body = (
        '<html><script>var a={"k":{"2003":[null,"tok",'
        '"https://example.com/page?x\\u003d1\\u0026y\\u003d2",'
        '"Caf\\u00e9 corner"]}};'
        "if(window.W_jd)for(var b in a)window.W_jd[b]=a[b];</script></html>"
    )
    results = parse_results(body, engine="google_state")
    assert results == [
        {"url": "https://example.com/page?x=1&y=2", "title": "Café corner", "snippet": ""}
    ]


def test_google_state_non_http_and_relative_urls_dropped():
    body = (
        "<html><script>var a={"
        '"a":{"2003":[null,"t1","/search?q=cache:x","Relative"]},'
        '"b":{"2003":[null,"t2","javascript:void(0)","JS href"]},'
        '"c":{"2003":[null,"t3","https://ok.example.com/page","Real"]}};'
        "if(window.W_jd)for(var b in a)window.W_jd[b]=a[b];</script></html>"
    )
    results = parse_results(body, engine="google_state")
    assert [r["url"] for r in results] == ["https://ok.example.com/page"]


def test_unknown_engine_still_raises_value_error():
    body = _load("ddg_lite_serp.html")
    for bad in ("google", "bing", "brave", "mojeek"):
        with pytest.raises(ValueError):
            parse_results(body, engine=bad)
