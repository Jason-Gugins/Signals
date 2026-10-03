#!/usr/bin/env python3
"""Offline re-analysis of Google SERP captures - ZERO network.

Task 1 of the Google SERP bypass ladder plan (2026-10-03). The prior probe
concluded "Google JS-gate holds headed" from ZERO *anchor* matches in the
751 KB headed-Patchright capture. Modern Google SERPs hydrate late and embed
result data as inline JS state (AF_initDataCallback chunks / window.google
blobs), and the /httpservice/retry/enablejs href lives in the standard
<noscript> block present on EVERY Google page. This analyzer settles - with
zero network requests - whether the existing captures actually contain
result data.

Per capture, reports:
  (a) <title> text
  (b) count of h3 elements (+ raw '<h3' cross-check)
  (c) count of external http(s) hrefs (non-google.com) + domain breakdown
  (d) presence and approximate size of AF_initDataCallback / window.google /
      W_jd-style embedded state blobs
  (e) result containers: id="search", id="r1"/id="rN", data-ved attributes
  (f) linkedin.com/in, /url?q=, uddg occurrences ANYWHERE in the raw bytes
  (g) captcha/consent overlay markers (g-recaptcha, /sorry/, consent.google.com,
      unusual traffic) + the JS-gate noscript/enablejs context
  (h) if result-like data IS found: up to 10 candidate result URLs + titles,
      printed as the actual strings found (DOM anchor pairs and state-blob
      pairs with a clearly-labeled heuristic title pairing)

Plain stdlib only (re, json, html.parser, urllib.parse). No external deps.

Usage:
    .venv/Scripts/python.exe scripts/analyze_google_shell.py <file.html> [more ...]
With no arguments it analyzes the known XRAY google captures under tmp/.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from html import unescape as html_unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlsplit

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_FILES = [
    "tmp/probe_xray_google_browser.html",
    "tmp/probe_xray_google_replay.html",
    "tmp/probe_xray_google.html",
    "tmp/probe_xray_diag_google_plain.html",
    "tmp/probe_xray_google_browser_cookies.json",
]

# Narrow exclusion for "external": only *.google.com hosts are google.
GOOGLE_HOST_SUFFIX = ".google.com"
GOOGLE_HOST_EXACT = "google.com"

RAW_MARKERS = [
    # (label, bytes pattern, case-insensitive)
    ("linkedin.com/in", b"linkedin.com/in", True),
    ("/url?q=", b"/url?q=", True),
    ("uddg", b"uddg", True),
    ("g-recaptcha", b"g-recaptcha", True),
    ("/sorry/", b"/sorry/", True),
    ("'sorry' (any)", b"sorry", True),
    ("consent.google.com", b"consent.google.com", True),
    ("unusual traffic", b"unusual traffic", True),
    ("recaptcha", b"recaptcha", True),
    ("httpservice/retry/enablejs", b"httpservice/retry/enablejs", True),
    ("<noscript", b"<noscript", True),
    ("data-ved", b"data-ved", True),
    ("AF_initDataCallback (raw hits)", b"AF_initDataCallback", False),
    ("window.google (raw hits)", b"window.google", False),
    ("W_jd", b"W_jd", False),
    ("MjjYud", b"MjjYud", False),
]

CONTAINER_PATTERNS = [
    ('id="search"', r'\bid\s*=\s*["\']?search["\']?'),
    ('id="r1"/id="rN"', r'\bid\s*=\s*["\']?r\d+["\']?'),
    ('data-ved attrs', r'\bdata-ved\s*='),
]

URL_RE = re.compile(r"https?://[^\s\"'<>\\)}\],]+")
QUOTED_RE = re.compile(r"([\"'])((?:\\.|(?!\1).){8,300}?)\1", re.S)
XESCAPE_RE = re.compile(r"\\x([0-9a-fA-F]{2})|\\u([0-9a-fA-F]{4})")


class CaptureParser(HTMLParser):
    """Collects titles, h3 elements, anchors (href+text), <noscript> count,
    and <script> block texts (for window.google blob sizing)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.titles: list[str] = []
        self._in_title = False
        self._title_buf: list[str] = []
        self.h3_count = 0
        self.h3_texts: list[str] = []
        self._in_h3 = 0
        self._h3_buf: list[str] = []
        self.anchors: list[tuple[str, str]] = []  # (href, text)
        self._in_anchor = 0
        self._anchor_href: str | None = None
        self._anchor_buf: list[str] = []
        self.noscript_count = 0
        self.scripts: list[str] = []
        self._in_script = False
        self._script_buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
            self._title_buf = []
        elif tag == "h3":
            self.h3_count += 1
            self._in_h3 += 1
            self._h3_buf = []
        elif tag == "a":
            self._in_anchor += 1
            href = dict(attrs).get("href")
            if href and self._anchor_href is None:
                self._anchor_href = href
                self._anchor_buf = []
        elif tag == "noscript":
            self.noscript_count += 1
        elif tag == "script":
            self._in_script = True
            self._script_buf = []

    def handle_endtag(self, tag):
        if tag == "title" and self._in_title:
            self._in_title = False
            self.titles.append("".join(self._title_buf).strip())
        elif tag == "h3" and self._in_h3 > 0:
            self._in_h3 -= 1
            if len(self.h3_texts) < 10:
                self.h3_texts.append(" ".join("".join(self._h3_buf).split()))
        elif tag == "a" and self._in_anchor > 0:
            self._in_anchor -= 1
            if self._anchor_href is not None:
                text = " ".join("".join(self._anchor_buf).split())
                self.anchors.append((self._anchor_href, text))
                self._anchor_href = None
                self._anchor_buf = []
        elif tag == "script" and self._in_script:
            self._in_script = False
            self.scripts.append("".join(self._script_buf))

    def handle_data(self, data):
        if self._in_title:
            self._title_buf.append(data)
        if self._in_h3 > 0:
            self._h3_buf.append(data)
        if self._anchor_href is not None and self._in_anchor > 0:
            self._anchor_buf.append(data)
        if self._in_script:
            self._script_buf.append(data)


def is_google_host(host: str) -> bool:
    host = (host or "").lower()
    return host == GOOGLE_HOST_EXACT or host.endswith(GOOGLE_HOST_SUFFIX)


def count_raw(data: bytes, pat: bytes, ci: bool) -> int:
    flags = re.IGNORECASE if ci else 0
    return len(re.findall(re.escape(pat), data, flags))


def find_af_blocks(data: bytes) -> list[tuple[str, int]]:
    """Locate AF_initDataCallback({...}) objects; return (key, approx size)."""
    blocks: list[tuple[str, int]] = []
    n = len(data)
    for m in re.finditer(rb"AF_initDataCallback\s*\(", data):
        j = data.find(b"{", m.end())
        if j == -1:
            continue
        depth = 0
        in_str = False
        quote = b'"'
        k = j
        while k < n:
            c = data[k : k + 1]
            if in_str:
                if c == b"\\":
                    k += 2
                    continue
                if c == quote:
                    in_str = False
            else:
                if c in (b'"', b"'"):
                    in_str = True
                    quote = c
                elif c == b"{":
                    depth += 1
                elif c == b"}":
                    depth -= 1
                    if depth == 0:
                        break
            k += 1
        end = min(k + 1, n)
        chunk = data[j:end]
        km = re.match(rb"\s*key:\s*'([^']*)'", chunk)
        key = km.group(1).decode("ascii", "replace") if km else "?"
        blocks.append((key, len(chunk)))
    return blocks


def js_unescape(s: str) -> str:
    """Decode \\xHH and \\uXXXX JS escapes so embedded URLs become greppable."""
    def repl(m: re.Match) -> str:
        h = m.group(1) or m.group(2)
        return chr(int(h, 16))

    return XESCAPE_RE.sub(repl, s)


def clean_url(u: str) -> str:
    u = u.rstrip(".,;:'\"")
    return u


def unwrap_google_redirect(href: str) -> str | None:
    """Unwrap /url?q=... and //duckduckgo.com/l/?uddg=... wrappers."""
    if "/url?" in href:
        try:
            q = parse_qs(urlsplit(href).query).get("q", [])
            if q:
                return q[0]
        except ValueError:
            return None
    if "uddg=" in href:
        try:
            q = parse_qs(urlsplit(href).query).get("uddg", [])
            if q:
                return unquote(q[0])
        except ValueError:
            return None
    return None


def nearby_title(unescaped: str, pos: int) -> str:
    """Heuristic: last quoted string in the 400 chars before the URL, else the
    first quoted string in the 300 chars after (excluding URL-ish strings).
    Prints actual strings found - pairing is labeled heuristic by the caller."""
    lo = max(0, pos - 400)
    before = unescaped[lo:pos]
    after = unescaped[pos : pos + 300]
    best = ""
    for m in QUOTED_RE.finditer(before):
        content = m.group(2)
        if "http" in content.lower():
            continue
        best = content
    if not best:
        for m in QUOTED_RE.finditer(after):
            content = m.group(2)
            if "http" in content.lower():
                continue
            best = content
            break
    if best:
        try:
            best = json.loads('"' + best.replace('"', '\\"').replace("\\''", "'") + '"')
        except Exception:
            pass
    return best


def sanitize(s: str) -> str:
    s = html_unescape(s)
    return re.sub(r"[\x00-\x1f\x7f]", " ", s)


def analyze_file(path: str) -> None:
    with open(path, "rb") as fh:
        data = fh.read()
    size = len(data)
    # latin-1 decode preserves byte<->char offsets for marker scans.
    text_l1 = data.decode("latin-1")

    print(f"=== {path} ({size:,} bytes) ===")

    # Informational JSON handling (cookies files etc.)
    if path.lower().endswith(".json"):
        try:
            obj = json.loads(text_l1)
            print("JSON document: parses OK")
            if isinstance(obj, dict):
                print(f"  top-level keys: {sorted(obj.keys())}")
                for k, v in obj.items():
                    if isinstance(v, list) and v and isinstance(v[0], dict) and "name" in v[0]:
                        names = [c.get("name", "?") for c in v]
                        print(f"  {k}: {len(v)} entries, names: {names}")
                    elif isinstance(v, list):
                        print(f"  {k}: {len(v)} entries")
            elif isinstance(obj, list):
                print(f"  JSON array of {len(obj)} entries")
            else:
                print(f"  JSON scalar: {type(obj).__name__}")
        except Exception as exc:
            print(f"JSON document: does NOT parse as JSON ({exc})")

    parser = CaptureParser()
    try:
        parser.feed(text_l1)
        parser.close()
    except Exception as exc:
        print(f"(HTMLParser warning: {exc})")

    # (a) title
    title = parser.titles[0] if parser.titles else "(none)"
    print(f"(a) <title>: {sanitize(title)!r}")

    # (b) h3
    raw_h3 = count_raw(data, b"<h3", False)
    print(f"(b) h3 elements: {parser.h3_count} (raw '<h3' count: {raw_h3})")
    for t in parser.h3_texts[:5]:
        if t:
            print(f"      h3 text: {sanitize(t)[:120]!r}")

    # (c) external hrefs
    ext_hosts: Counter = Counter()
    ext_hrefs: list[tuple[str, str]] = []
    total_https_hrefs = 0
    redirect_hrefs = 0
    for href, text in parser.anchors:
        low = href.lower()
        if low.startswith("http://") or low.startswith("https://"):
            total_https_hrefs += 1
            host = urlsplit(href).netloc.lower()
            if not is_google_host(host):
                ext_hosts[host] += 1
                ext_hrefs.append((href, text))
        unwrapped = unwrap_google_redirect(href)
        if unwrapped:
            redirect_hrefs += 1
            uhost = urlsplit(unwrapped).netloc.lower() if "://" in unwrapped else ""
            if uhost and not is_google_host(uhost):
                ext_hosts[uhost] += 1
                ext_hrefs.append((unwrapped, text))
    print(
        f"(c) external http(s) hrefs (non-google.com): {sum(ext_hosts.values())}"
        f" | total http(s) hrefs: {total_https_hrefs} | wrapped /url?q=|uddg= hrefs: {redirect_hrefs}"
    )
    if ext_hosts:
        top = ", ".join(f"{h} ({c})" for h, c in ext_hosts.most_common(15))
        print(f"      external domains: {top}")

    # (d) embedded state blobs
    af_blocks = find_af_blocks(data)
    af_total = sum(sz for _, sz in af_blocks)
    af_keys = [k for k, _ in af_blocks]
    wg_hits = count_raw(data, b"window.google", False)
    wg_script_bytes = sum(len(s) for s in parser.scripts if "window.google" in s)
    wjd_hits = count_raw(data, b"W_jd", False)
    script_total = sum(len(s) for s in parser.scripts)
    print(
        f"(d) AF_initDataCallback: {len(af_blocks)} block(s), {af_total:,} bytes total"
        + (f", keys={af_keys[:20]}" if af_keys else "")
    )
    print(
        f"      window.google: {wg_hits} mention(s), ~{wg_script_bytes:,} bytes of <script> containing it"
        f" | W_jd: {wjd_hits} hit(s) | total <script> bytes: {script_total:,}"
    )

    # (e) result containers
    cont_parts = []
    for label, pat in CONTAINER_PATTERNS:
        cont_parts.append(f'{label}: {len(re.findall(pat, text_l1))}')
    print("(e) result containers -> " + " | ".join(cont_parts))

    # (f) raw-byte markers
    print("(f) raw-byte markers:")
    linkedin_hits = count_raw(data, b"linkedin.com/in", True)
    urlq_hits = count_raw(data, b"/url?q=", True)
    uddg_hits = count_raw(data, b"uddg", True)
    print(
        f"      linkedin.com/in: {linkedin_hits} | /url?q=: {urlq_hits} | uddg: {uddg_hits}"
    )

    # (g) overlay markers
    ov = {label: count_raw(data, pat, ci) for label, pat, ci in RAW_MARKERS if pat in (
        b"g-recaptcha", b"/sorry/", b"consent.google.com", b"unusual traffic", b"recaptcha")}
    print(
        "(g) overlay markers -> g-recaptcha: {g} | /sorry/: {s} | consent.google.com: {c}"
        " | unusual traffic: {u} | recaptcha: {r}".format(
            g=ov.get("g-recaptcha", 0), s=ov.get("/sorry/", 0), c=ov.get("consent.google.com", 0),
            u=ov.get("unusual traffic", 0), r=ov.get("recaptcha", 0))
    )
    print(
        f"      JS-gate context -> httpservice/retry/enablejs: {count_raw(data, b'httpservice/retry/enablejs', True)}"
        f" | <noscript: {count_raw(data, b'<noscript', True)} | X-Sorry-Redirect: {count_raw(data, b'X-Sorry-Redirect', True)}"
    )

    # (h) candidate result URLs + titles
    # Group 1: DOM anchor pairs (real rendered results if any).
    dom_candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    for href, text in parser.anchors:
        url = unwrap_google_redirect(href) or (
            href if href.lower().startswith(("http://", "https://")) else None
        )
        if not url:
            continue
        host = urlsplit(url).netloc.lower() if "://" in url else ""
        if is_google_host(host):
            continue
        if url not in seen:
            seen.add(url)
            dom_candidates.append((url, text))

    # Group 2: URLs embedded in raw bytes / state blobs (results as data).
    unescaped = js_unescape(text_l1)
    blob_candidates: list[tuple[str, str]] = []
    blob_url_count = 0
    for m in URL_RE.finditer(unescaped):
        url = clean_url(m.group(0))
        host = urlsplit(url).netloc.lower() if "://" in url else ""
        if not host or is_google_host(host):
            continue
        blob_url_count += 1
        if url not in seen and len(blob_candidates) < 10:
            seen.add(url)
            blob_candidates.append((url, nearby_title(unescaped, m.start())))

    print(
        f"(h) result-like data: DOM external anchors: {len(dom_candidates)}"
        f" | unique non-google URLs in raw/state bytes: {blob_url_count}"
    )

    def dump(label: str, cands: list[tuple[str, str]]) -> None:
        if not cands:
            return
        print(f"      {label}:")
        for url, t in cands[:10]:
            print(f"        URL: {sanitize(url)[:200]}")
            print(f"        TITLE: {sanitize(t)[:160]!r}")

    dump("DOM anchor candidates (href + anchor text)", dom_candidates)
    dump("State-blob candidates (URL + heuristic nearby quoted string)", blob_candidates)

    print()


def main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    files = argv[1:]
    if not files:
        files = [os.path.join(REPO_ROOT, rel) for rel in DEFAULT_FILES]
    missing = [f for f in files if not os.path.isfile(f)]
    for f in missing:
        print(f"=== {f}: MISSING ===\n")
    for f in files:
        if os.path.isfile(f):
            analyze_file(f)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
