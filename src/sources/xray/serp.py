# src/sources/xray/serp.py
"""PURE SERP-body parsers + challenge detection for the X-ray runner.

Two built engines (Google bypass ladder Task 6, verdicts in
data/probe/XRAY_SERP_2026_10.md):

- ``ddg_lite`` (DEFAULT) — GET ``https://lite.duckduckgo.com/lite/?q=<encoded>``
  via the curl_cffi chrome-TLS tier. The lite endpoint is STOCHASTIC on
  operator queries: it serves 202 anomaly challenges and 403 error-lites on
  some requests, so every body is classified BEFORE parsing (body-validated,
  never status-validated — ddg_ids.py lesson: challenge bodies ship with
  HTTP 200 AND 202 alike).
- ``google_state`` — Google serves its organic result set as EMBEDDED JS
  STATE: the headed-browser and fresh-cookie-replay captures carry 12
  plaintext ``"2003":[null,"<tok>","<url>","<title>",...]`` records inside
  the ``window.W_jd`` window-state blob, while every rendered anchor href is
  an opaque relative ``/goto?url=<b64>`` blob (the Task 1 "measurement
  artifact" — zero external anchors, full result set). Parsing therefore
  regexes the W_jd records, not anchors. Snippet is ALWAYS "" (the records
  carry url+title only; hits.py works on titles/URLs so company/profile
  extraction is unaffected). Fetching this engine REQUIRES a fresh
  browser-harvested cookie jar (per-session — cookies do NOT amortize ≥1
  day; the fetch layer owns that, see cli._xray_google_state_fetch).

Real lite markup (recorded from tmp/probe_xray_ddg_lite_q1.html, the only
organic operator-query body captured; the lite endpoint differs from the
html endpoint — do NOT assume ``result__a``):

    <a rel="nofollow"
       href="//duckduckgo.com/l/?uddg=<urlencoded>&amp;rut=<hash>"
       class='result-link'>Title</a>
    ...next <tr>...
    <td class='result-snippet'>snippet text with <b>hits</b></td>

i.e. one ``result-link`` anchor per result wrapped in a ``uddg=`` redirect,
each followed (in the plain control, 10/10) by one ``result-snippet`` cell.
Unwrapping: the ``uddg`` param is percent-encoded TWICE in the wild
(``%2D`` for dashes survives one decode), so the value is unquoted once by
``parse_qs`` and once more explicitly — the same shape
``src/identity/ddg_ids.py:_resolve_href`` uses.

Purity: no I/O, no clock, no network imports — the runner task owns fetch,
pacing and persistence. Tolerant in one direction only: a clean body with
zero extractable results raises ``ParseError`` instead of returning an empty
list — silent empties look like "no leads" and poison the ledger.
"""
from __future__ import annotations

import re
from html import unescape
from urllib.parse import parse_qs, unquote, urlsplit

SUPPORTED_ENGINES = ("ddg_lite", "google_state")

# Challenge/block markers, case-insensitive, most specific first (the first
# hit is the recorded marker).
#
# DDG markers are in lockstep with src/identity/ddg_ids.py — the specific
# modal copy ("unfortunately, bots use duckduckgo", "anomaly-detected") and
# the error-lite hard-403 shape ("error-lite", the ONLY marker in that body).
# Wave-1 review REMOVED the bare words "anomaly" and "challenge": as plain
# substrings over the whole body (snippets included) they false-positive on
# organic hiring/intent copy ("we love a challenge", "anomaly detection")
# while adding nothing — the 202 modal matches the specific copy and the 403
# matches "error-lite" (data/probe/XRAY_SERP_2026_10.md).
_DDG_CHALLENGE_MARKERS: tuple[str, ...] = (
    "unfortunately, bots use duckduckgo",
    "anomaly-detected",
    "captcha",
    "error-lite",
)

# Google markers — verified against the live bodies in
# data/probe/XRAY_SERP_2026_10.md: the 429 captcha interstitial (ladder Tasks
# 2+3) carries "unusual traffic" + "g-recaptcha"/"recaptcha" (the bare
# "captcha" DDG marker also matches), so the existing list covers it — no new
# google-specific marker was documented. Two recorded caveats: (a) the
# enablejs noscript href ALSO appears on genuine result bodies (Task 1) — it
# only stays honest because is_challenge scans the front-loaded 20k window
# and real result pages carry it at ~141k; (b) the /sorry/ + X-Sorry-Redirect
# strings in result captures are the app shell's own guard JS (never a served
# body) — do NOT add X-Sorry-Redirect as a marker.
_GOOGLE_CHALLENGE_MARKERS: tuple[str, ...] = (
    "/httpservice/retry/enablejs",
    "/sorry/",
    "unusual traffic",
    "consent.google.com",
    "g-recaptcha",
    "recaptcha",
)

CHALLENGE_MARKERS: tuple[str, ...] = (
    _DDG_CHALLENGE_MARKERS + _GOOGLE_CHALLENGE_MARKERS
)

_IS_CHALLENGE_WINDOW = 20_000  # chars scanned; challenge pages front-load markers


class ParseError(ValueError):
    """A SERP body could not be parsed into organic results."""


def is_challenge(body: str) -> str | None:
    """First challenge marker in the body's first 20k chars, or None (pure).

    Case-insensitive; returns the marker string (never a bare bool) so the
    ledger can record WHY a body was rejected.
    """
    low = (body or "")[:_IS_CHALLENGE_WINDOW].casefold()
    for marker in CHALLENGE_MARKERS:
        if marker in low:
            return marker
    return None


_ANCHOR_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.S | re.I)
_HREF_RE = re.compile(r"""href\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)
_TD_RE = re.compile(r"<td\b([^>]*)>(.*?)</td>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# Result anchors: the lite endpoint's own class, or any anchor that carries
# a uddg redirect (the organic-click wrapper) even if the class drifts.
_ANCHOR_ATTR_MARKERS = ("result-link", "uddg=")

# Ad/redirect junk hints inside the raw href (checked before unwrapping).
_AD_HINTS: tuple[str, ...] = ("y.js", "ad_provider", "ad_domain", "ad_script")

_INTERNAL_HOST = "duckduckgo.com"

_TITLE_MAX = 300
_SNIPPET_MAX = 500


def _text(fragment: str) -> str:
    """Tag-stripped, entity-decoded, whitespace-collapsed text (pure)."""
    plain = unescape(_TAG_RE.sub(" ", fragment or ""))
    return _WS_RE.sub(" ", plain).strip()


def _unwrap_href(href: str) -> str | None:
    """Result anchor href -> real target URL (pure).

    Handles ``//duckduckgo.com/l/?uddg=<encoded>&rut=...`` wrappers (the uddg
    param is doubly percent-encoded in the wild — parse_qs decodes once,
    unquote again), scheme-relative and root-relative hrefs. Direct http(s)
    hrefs pass through.
    """
    value = unescape((href or "").strip())
    if not value:
        return None
    if value.startswith("//"):
        value = "https:" + value
    elif value.startswith("/"):
        value = f"https://{_INTERNAL_HOST}{value}"
    if _INTERNAL_HOST + "/l/" in value.casefold():
        try:
            query = urlsplit(value).query
        except ValueError:
            return value
        target = (parse_qs(query).get("uddg") or [None])[0]
        if target:
            return unquote(target)
    return value


def _is_ad_href(href: str) -> bool:
    low = (href or "").casefold()
    return any(hint in low for hint in _AD_HINTS)


def _is_internal(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").casefold()
    except ValueError:
        return True
    return host == _INTERNAL_HOST or host.endswith("." + _INTERNAL_HOST)


def parse_results(body: str, *, engine: str = "ddg_lite") -> list[dict]:
    """Organic results as ``[{"url", "title", "snippet"}]`` (pure).

    Dispatches per engine: ``ddg_lite`` extracts ``result-link`` anchors from
    the lite markup; ``google_state`` extracts the ``window.W_jd`` "2003"
    url+title records (snippet always ""). Deduped by final URL, in document
    order. Internal engine links and ad hrefs are dropped. Raises
    ``ParseError`` on a challenge body (marker carried in the message) and on
    a clean body with zero extractable results — never returns a silent empty
    list.
    """
    if engine not in SUPPORTED_ENGINES:
        raise ValueError(
            f"unsupported engine {engine!r}: built engines are {SUPPORTED_ENGINES} "
            "(bing/brave/mojeek probed NO-GO per data/probe/XRAY_SERP_2026_10.md)"
        )
    marker = is_challenge(body)
    if marker is not None:
        raise ParseError(f"{engine}: challenge page served ({marker!r} in body)")
    if engine == "google_state":
        return _parse_google_state(body)

    anchors: list[tuple[int, str, str]] = []
    for match in _ANCHOR_RE.finditer(body or ""):
        attrs = match.group(1) or ""
        low = attrs.casefold()
        if not any(m in low for m in _ANCHOR_ATTR_MARKERS):
            continue
        href_match = _HREF_RE.search(attrs)
        if href_match is None:
            continue
        href = next(g for g in href_match.groups() if g is not None)
        anchors.append((match.start(), href, match.group(2) or ""))

    snippets: list[tuple[int, str]] = []
    for match in _TD_RE.finditer(body or ""):
        if "result-snippet" not in (match.group(1) or "").casefold():
            continue
        snippets.append((match.start(), _text(match.group(2) or "")))

    results: list[dict] = []
    seen: set[str] = set()
    for idx, (pos, href, inner) in enumerate(anchors):
        if _is_ad_href(href):
            continue
        url = _unwrap_href(href)
        if not url or not url.casefold().startswith(("http://", "https://")):
            continue
        if _is_internal(url):
            continue
        if url in seen:
            continue
        seen.add(url)
        next_pos = anchors[idx + 1][0] if idx + 1 < len(anchors) else float("inf")
        snippet = next(
            (text for spos, text in snippets if pos < spos < next_pos and text), ""
        )
        results.append(
            {
                "url": url,
                "title": _text(inner)[:_TITLE_MAX],
                "snippet": snippet[:_SNIPPET_MAX],
            }
        )
    if not results:
        raise ParseError(f"{engine}: no organic anchors parsed from body")
    return results


# --- google_state: the window.W_jd embedded-state parser --------------------
#
# Extraction approach mirrors scripts/analyze_google_shell.py (the Task 1
# analyzer): regex the raw body for the plaintext "2003" records — zero DOM
# dependency, full plaintext URLs — and decode the JS \\xHH / \\uXXXX escapes
# so escaped URLs become real. Record shape (verbatim from the captures):
#
#     "2003":[null,"<tok>","<url>","<title>", ...more elements...]
#
# The rendered anchors are useless for this engine: every href is an opaque
# relative "/goto?url=<b64>" blob, so the state blob is the ONLY url+title
# source.

# The state blob's name — present on every result-bearing capture (Task 1:
# 21 hits on the 751KB body, 0 on the 93KB cookie-less shells). Its presence
# plus zero extractable records still raises (a torn/unknown layout must
# never pass as "no leads").
_WJD_STATE_RE = re.compile(r"W_jd")

# ["2003":[null,"<tok>","<url>","<title>" — escape-aware quoted fields.
_WJD_RECORD_RE = re.compile(
    r'"2003"\s*:\s*\[\s*null\s*,\s*"[^"]*"\s*,\s*'
    r'"(?P<url>(?:\\.|[^"\\])*)"\s*,\s*'
    r'"(?P<title>(?:\\.|[^"\\])*)"'
)

# JS string escapes (analyze_google_shell.js_unescape) + the JSON "\/" form.
_XESCAPE_RE = re.compile(r"\\x([0-9a-fA-F]{2})|\\u([0-9a-fA-F]{4})")

_INTERNAL_GOOGLE_HOST = "google.com"

_WJD_TITLE_MAX = 300


def _js_unescape(value: str) -> str:
    r"""Decode \\xHH / \\uXXXX JS escapes and the JSON "\/" form (pure)."""

    def repl(match: re.Match) -> str:
        return chr(int(match.group(1) or match.group(2), 16))

    return _XESCAPE_RE.sub(repl, value).replace("\\/", "/")


def _is_google_internal(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").casefold()
    except ValueError:
        return True
    return host == _INTERNAL_GOOGLE_HOST or host.endswith("." + _INTERNAL_GOOGLE_HOST)


def _parse_google_state(body: str) -> list[dict]:
    """``window.W_jd`` "2003" records -> ``[{"url", "title", "snippet"}]``.

    Snippet is ALWAYS "": the embedded records carry url+title only (a
    documented engine limitation — hits.py works on titles/URLs, so company
    and profile extraction are unaffected). Records are returned in document
    order, deduped by URL; relative/non-http and google-internal URLs are
    dropped. Missing W_jd state or zero records raises ``ParseError`` —
    silent empties poison the ledger.
    """
    if not _WJD_STATE_RE.search(body or ""):
        raise ParseError("google_state: no result records parsed from body")

    results: list[dict] = []
    seen: set[str] = set()
    for match in _WJD_RECORD_RE.finditer(body or ""):
        url = _js_unescape(match.group("url")).strip()
        if not url or not url.casefold().startswith(("http://", "https://")):
            continue
        if _is_google_internal(url) or url in seen:
            continue
        seen.add(url)
        title = _text(_js_unescape(match.group("title")))[:_WJD_TITLE_MAX]
        results.append({"url": url, "title": title, "snippet": ""})
    if not results:
        raise ParseError("google_state: no result records parsed from body")
    return results
