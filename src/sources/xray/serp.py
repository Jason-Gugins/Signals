# src/sources/xray/serp.py
"""PURE DDG-lite SERP-body parser + challenge detection for the X-ray runner.

Re-scoped by the Task 3/3b probe verdict (data/probe/XRAY_SERP_2026_10.md):
google is a final NO-GO on every transport including the browser tier (the
200 JS-gate app shell never renders anchors), so the ONLY built engine is
``ddg_lite`` — GET ``https://lite.duckduckgo.com/lite/?q=<encoded>`` via the
curl_cffi chrome-TLS tier. The lite endpoint is STOCHASTIC on operator
queries: it serves 202 anomaly challenges and 403 error-lites on some
requests, so every body is classified BEFORE parsing (body-validated, never
status-validated — ddg_ids.py lesson: challenge bodies ship with HTTP 200
AND 202 alike).

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
zero extractable organic anchors raises ``ParseError`` instead of returning
an empty list — silent empties look like "no leads" and poison the ledger.
"""
from __future__ import annotations

import re
from html import unescape
from urllib.parse import parse_qs, unquote, urlsplit

SUPPORTED_ENGINES = ("ddg_lite",)

# Challenge/block markers, case-insensitive, most specific first (the first
# hit is the recorded marker).
#
# DDG markers are in lockstep with src/identity/ddg_ids.py:_CHALLENGE_MARKERS
# ("unfortunately, bots use duckduckgo", "anomaly-detected", "anomaly",
# "captcha") — extended with the two shapes the probe observed that the html
# endpoint never serves: the bare word "challenge" (anomaly-modal copy) and
# "error-lite" (the ONLY marker in the hard-403 error-lite body, whose mailto
# is error-lite+...@duckduckgo.com; data/probe/XRAY_SERP_2026_10.md).
_DDG_CHALLENGE_MARKERS: tuple[str, ...] = (
    "unfortunately, bots use duckduckgo",
    "anomaly-detected",
    "anomaly",
    "captcha",
    "error-lite",
    "challenge",
)

# Google markers — google is a documented NO-GO on every transport including
# the browser tier (probe verdict: the 200 JS-gate shell carries zero organic
# anchors and no classic captcha markers; /httpservice/retry/enablejs is the
# only stable block signal). Detection STAYS in the list so a future engine
# swap inherits honest classification instead of parsing block shells.
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

    Deduped by final (unwrapped) URL, in document order. Internal
    duckduckgo.com links and ad hrefs are dropped. Raises ``ParseError`` on a
    challenge body (marker carried in the message) and on a clean body with
    zero organic anchors — never returns a silent empty list.
    """
    if engine not in SUPPORTED_ENGINES:
        raise ValueError(
            f"unsupported engine {engine!r}: only {SUPPORTED_ENGINES} is built "
            "(google is NO-GO per data/probe/XRAY_SERP_2026_10.md)"
        )
    marker = is_challenge(body)
    if marker is not None:
        raise ParseError(f"{engine}: challenge page served ({marker!r} in body)")

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
