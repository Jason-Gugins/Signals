"""Google cookie solve -> harvest -> replay-verify pipeline (XRAY bypass ladder
Tasks 2+3, re-scoped by Task 1's RESULTS_AS_DATA verdict, 2026-10-03).

Task 1 (commit e15b8bb) proved the headed-Patchright capture and the
browser-cookie replay capture BOTH carry a full organic SERP as embedded state
(window.W_jd ``"2003":[null,"<tok>","<url>","<title>",...]`` records plus 9
rendered h3 tiles), and that result links are opaque relative
``/goto?url=<b64>`` hrefs -- DOM anchor counting was the measurement artifact.
The original ladder Task 2 (12-cell no-cookie matrix) and Task 3 (4 escalation
variants) are therefore mostly answered or foregone. This ONE script answers
the re-scoped questions with minimal requests:

  Q1  Do yesterday's harvested cookies still yield a result-bearing body
      TODAY?  curl_cffi Chrome/147 GET /search?q=<operator query>&hl=en with
      the name+value pairs from tmp/probe_xray_google_browser_cookies.json.
  Q2  The three never-tried cells, each once: (a) old-Firefox UA no cookies on
      the plain /search?q= URL, (b) iPhone Safari UA no cookies, (c) yesterday
      cookies + gbv=1. curl_cffi chrome-TLS throughout (note honestly: for
      (a)/(b) TLS says Chrome while UA says Firefox/iPhone -- the cell still
      answers whether the SERVER serves result/basic HTML to that UA).
  Q3  The refresh procedure, formalized and repeatable: Patchright headed
      solve of the operator query (~25s settle), verify result presence with
      the analyzer checks, harvest context.cookies() to
      data/xray/google_cookies.json (data/ is gitignored), then IMMEDIATELY
      curl_cffi replay-verify the fresh jar -- proving solve -> harvest ->
      replay end-to-end today.

Classification is body-validated with the Task 1 analyzer's checks (NEVER by
status, and NEVER the enablejs noscript href alone -- that href is present on
EVERY google page including result-bearing ones). A body is RESULTS iff it
carries >=1 W_jd "2003" record OR >=1 rendered h3 OR >=3 external result URLs;
EMPTY iff all three are zero; CHALLENGE only when a hard marker appears with
no result data. The >=3 bar is the escalation rung's anchors>=3 rule re-derived
the hard way: block pages link only to their own properties, and the
unusual-traffic captcha interstitial's lone external URL is the gstatic
recaptcha JS -- one external URL is context, never a result set.

Budget (hard): <=8 HTTP requests and <=3 browser page loads per invocation;
>=4 s pacing between ANY two google requests (same-host rule is global here
because every cell hits www.google.com); 2-consecutive-challenge abort for
HTTP cells (the Q3 browser solve still runs -- it IS the refresh procedure);
zero retries of hard blocks.

Usage (repo root, venv python):
  .venv/Scripts/python.exe scripts/google_cookie_refresh.py all        # Q1 -> Q2 -> Q3
  .venv/Scripts/python.exe scripts/google_cookie_refresh.py replay --cookies tmp/probe_xray_google_browser_cookies.json
  .venv/Scripts/python.exe scripts/google_cookie_refresh.py ua-cells  [--cookies PATH]
  .venv/Scripts/python.exe scripts/google_cookie_refresh.py refresh   # Q3 only
  .venv/Scripts/python.exe scripts/google_cookie_refresh.py selftest  # offline: classify known captures, ZERO network

Writes (tmp/ and data/ are gitignored; never committed):
  data/xray/google_cookies.json             fresh refresh jar (default)
  tmp/probe_xray_google_replay2.html        result-bearing replay body (Q1; Q3 uses _fresh if taken)
  tmp/probe_xray_google_replay_fresh.html   Q3 fresh-jar replay body
  tmp/probe_xray_google_refresh_solve.html  Q3 browser-solve DOM
  tmp/probe_xray_google_ua_firefox60.html   Q2 cell bodies (one per cell)
  tmp/probe_xray_google_ua_iphone.html
  tmp/probe_xray_google_cookies_gbv1.html
"""

from __future__ import annotations

import json
import re
import sys
import time
from html import unescape as html_unescape
from pathlib import Path
from urllib.parse import quote_plus, unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# --- constants -----------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
TMP_DIR = REPO_ROOT / "tmp"
DEFAULT_JAR = REPO_ROOT / "data" / "xray" / "google_cookies.json"
YESTERDAY_JAR = REPO_ROOT / "tmp" / "probe_xray_google_browser_cookies.json"

OPERATOR_QUERY = 'site:linkedin.com/in "head of growth" "Austin"'

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)
OLD_FIREFOX_UA = "Mozilla/5.0 (Windows NT 6.1; rv:60.0) Gecko/20100101 Firefox/60.0"
IPHONE_SAFARI_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 "
    "(Version/16.6 Mobile/15E148 Safari/604.1)"
)

HTTP_BUDGET = 8        # hard cap, whole invocation
LOAD_BUDGET = 3        # browser page loads, whole invocation
PACE_S = 4.0           # between ANY two google requests (incl. browser gotos)
SETTLE_S = 25.0        # browser JS settle window (escalation-rung precedent)
GOTO_TIMEOUT_MS = 40_000
CHALLENGE_WALL = 2     # consecutive challenge bodies -> abort remaining HTTP cells

# Hard challenge markers only. /httpservice/retry/enablejs is deliberately NOT
# here: Task 1 established it sits in the standard <noscript> of EVERY google
# page, result-bearing ones included.
HARD_MARKERS = (
    "/sorry/",
    "unusual traffic",
    "g-recaptcha",
    "recaptcha",
    "consent.google.com",
)

# Hosts whose URLs are markup boilerplate (XML namespaces etc.), never results.
NON_RESULT_HOSTS = ("w3.org", "schema.org", "purl.org", "xmlsoap.org")

URL_RE = re.compile(r"https?://[^\s\"'<>\\)}\],]+")
HREF_RE = re.compile(r"href=\"([^\"]+)\"", re.I)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
REC_2003_RE = re.compile(r"\"2003\":\[")
H3_RE = re.compile(r"<h3[\s>]", re.I)
LINKEDIN_RE = re.compile(r"linkedin\.com/in", re.I)
XESCAPE_RE = re.compile(r"\\x([0-9a-fA-F]{2})|\\u([0-9a-fA-F]{4})")

# Offline selftest fixtures with ground truth (verdict + exact metrics).
# First four: the Task 1 analyzer's verified numbers on the original captures.
# Latter six: the 2026-10-03 live-run bodies -- the unusual-traffic captcha
# (429 replays + browser interstitial, whose lone external URL is the gstatic
# recaptcha JS) must NEVER classify as RESULTS, and the two UA shells stay EMPTY.
SELFTEST_FILES = [
    ("tmp/probe_xray_google_browser.html", "RESULTS",
     {"h3": 9, "wjd": 21, "linkedin_hits": 104, "rec2003": 12}),
    ("tmp/probe_xray_google_replay.html", "RESULTS",
     {"h3": 9, "wjd": 21, "linkedin_hits": 91, "rec2003": 12}),
    ("tmp/probe_xray_google.html", "EMPTY",
     {"h3": 0, "wjd": 0, "linkedin_hits": 1, "rec2003": 0}),
    ("tmp/probe_xray_diag_google_plain.html", "EMPTY",
     {"h3": 0, "wjd": 0, "linkedin_hits": 0, "rec2003": 0}),
    ("tmp/probe_xray_google_replay_q1.html", "CHALLENGE",
     {"h3": 0, "wjd": 0, "linkedin_hits": 0, "rec2003": 0}),
    ("tmp/probe_xray_google_cookies_gbv1.html", "CHALLENGE",
     {"h3": 0, "wjd": 0, "linkedin_hits": 0, "rec2003": 0}),
    ("tmp/probe_xray_google_replay_fresh.html", "CHALLENGE",
     {"h3": 0, "wjd": 0, "linkedin_hits": 0, "rec2003": 0}),
    ("tmp/probe_xray_google_refresh_solve.html", "CHALLENGE",
     {"h3": 0, "wjd": 0, "linkedin_hits": 0, "rec2003": 0}),
    ("tmp/probe_xray_google_ua_firefox60.html", "EMPTY",
     {"h3": 0, "wjd": 0, "linkedin_hits": 1, "rec2003": 0}),
    ("tmp/probe_xray_google_ua_iphone.html", "EMPTY",
     {"h3": 0, "wjd": 0, "linkedin_hits": 1, "rec2003": 0}),
]

# --- budget ledger + pacing ------------------------------------------------------

_LEDGER = {"http": 0, "loads": 0, "per_host": {}, "challenge_streak": 0, "aborted": False}
_LAST_REQUEST_AT = 0.0


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "?").lower()
    except ValueError:
        return "?"


def pace() -> None:
    """Sleep so consecutive requests are >= PACE_S apart (global, monotonic)."""
    global _LAST_REQUEST_AT
    wait = PACE_S - (time.monotonic() - _LAST_REQUEST_AT)
    if wait > 0:
        time.sleep(wait)
    _LAST_REQUEST_AT = time.monotonic()


def _count_request(url: str) -> None:
    _LEDGER["http"] += 1
    host = _host(url)
    _LEDGER["per_host"][host] = _LEDGER["per_host"].get(host, 0) + 1


def _count_load(url: str) -> None:
    _LEDGER["loads"] += 1
    key = f"{_host(url)} (page load)"
    _LEDGER["per_host"][key] = _LEDGER["per_host"].get(key, 0) + 1


# --- body classification (Task 1 analyzer checks, inline) -------------------------

def _js_unescape(s: str) -> str:
    def repl(m: re.Match) -> str:
        h = m.group(1) or m.group(2)
        return chr(int(h, 16))

    return XESCAPE_RE.sub(repl, s)


def _is_google_url(u: str) -> bool:
    h = _host(u)
    return h == "google.com" or h.endswith(".google.com")


def _clean_url(u: str) -> str:
    return u.rstrip(".,;:\"'")


def _is_noise_host(u: str) -> bool:
    h = _host(u)
    return any(h == n or h.endswith("." + n) for n in NON_RESULT_HOSTS)


def classify_body(data: bytes) -> dict:
    """Analyze one google body with the Task 1 analyzer's checks.

    Returns metrics + verdict. RESULTS iff rec2003 records OR h3 OR external
    result URLs; EMPTY iff all three are zero; CHALLENGE only on a hard marker
    with no result data. enablejs/noscript are reported as context, never as
    block evidence (present on result pages too).
    """
    text = data.decode("latin-1")
    unesc = _js_unescape(text)

    h3 = len(H3_RE.findall(text))
    wjd = text.count("W_jd")

    # W_jd "2003":[null,"<tok>","<url>","<title>",...] state records
    rec_chunks: list[str] = []
    for m in REC_2003_RE.finditer(text):
        chunk = text[m.end() : m.end() + 1500]
        end = chunk.find("]")
        if end != -1:
            chunk = chunk[:end]
        rec_chunks.append(_js_unescape(chunk))
    rec_urls: set[str] = {
        _clean_url(u)
        for ch in rec_chunks
        for u in URL_RE.findall(ch)
        if not _is_google_url(u)
    }

    # external result URLs: union of href attrs (URL-decoded), raw-byte URLs
    # (JS-unescaped), and "2003"-record URLs. Relative /goto?url=<b64> hrefs
    # are opaque and contribute nothing -- by design; h3/rec2003 cover them.
    ext: set[str] = set(rec_urls)
    for href in HREF_RE.findall(text):
        for cand in (href, unquote(href)):
            for u in URL_RE.findall(cand):
                cu = _clean_url(u)
                if not _is_google_url(cu):
                    ext.add(cu)
    for u in URL_RE.findall(unesc):
        cu = _clean_url(u)
        if not _is_google_url(cu):
            ext.add(cu)
    ext = {u for u in ext if not _is_noise_host(u)}

    linkedin_paths = sorted({u for u in ext if "linkedin.com/in" in u.lower()})
    linkedin_hits = len(LINKEDIN_RE.findall(text))

    tmatch = TITLE_RE.search(text)
    title = html_unescape(tmatch.group(1)).strip() if tmatch else ""

    low = text.casefold()
    hard = [m for m in HARD_MARKERS if m in low]

    is_results = h3 > 0 or len(rec_chunks) > 0 or len(ext) >= 3
    verdict = "RESULTS" if is_results else ("CHALLENGE" if hard else "EMPTY")
    return {
        "verdict": verdict,
        "bytes": len(data),
        "title": title,
        "h3": h3,
        "wjd": wjd,
        "rec2003": len(rec_chunks),
        "rec_urls": sorted(rec_urls),
        "ext_urls": sorted(ext),
        "linkedin_paths": linkedin_paths,
        "linkedin_hits": linkedin_hits,
        "hard_markers": hard,
        "enablejs": text.casefold().count("httpservice/retry/enablejs"),
    }


def fmt_metrics(m: dict) -> str:
    s = (
        f"{m['verdict']:9} bytes={m['bytes']:,} h3={m['h3']} "
        f"2003rec={m['rec2003']} wjd={m['wjd']} linkedin_hits={m['linkedin_hits']} "
        f"linkedin_paths={len(m['linkedin_paths'])} ext_urls={len(m['ext_urls'])} "
        f"title={m['title'][:60]!r}"
    )
    if m.get("hard_markers"):
        s += f" markers={m['hard_markers']}"
    return s


# --- cookie jars ------------------------------------------------------------------

def load_jar(path: Path) -> list[dict]:
    """Load a harvested cookie JSON list; keep google.com cookies as name+value."""
    cookies = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(cookies, dict):  # tolerate {"cookies": [...]} wrappers
        cookies = cookies.get("cookies", [])
    return [
        {"name": c["name"], "value": c["value"]}
        for c in cookies
        if c.get("name") and "google.com" in (c.get("domain") or "").lower()
    ]


def jar_summary(cookies: list[dict]) -> str:
    return f"{len(cookies)} cookies: {', '.join(c['name'] for c in cookies)}"


# --- HTTP cells --------------------------------------------------------------------

def _save_body(m: dict, raw: bytes, name: str) -> str:
    TMP_DIR.mkdir(exist_ok=True)
    p = TMP_DIR / name
    p.write_bytes(raw)
    return str(p)


def http_cell(label: str, url: str, ua: str, cookies: list[dict] | None,
              save_name: str, replay2_ok: bool) -> tuple[dict, bytes, bool]:
    """One paced, budgeted curl_cffi cell. Returns (metrics, raw, replay2_still_free).

    Never retries; updates the challenge streak (2 consecutive -> abort HTTP cells).
    """
    if _LEDGER["aborted"]:
        print(f"  [{label}] ABORTED — challenge wall hit earlier in this run")
        return {"verdict": "ABORTED", "bytes": 0, "h3": 0, "rec2003": 0,
                "linkedin_hits": 0, "ext_urls": [], "title": ""}, b"", replay2_ok
    if _LEDGER["http"] >= HTTP_BUDGET:
        print(f"  [{label}] SKIPPED — HTTP budget exhausted")
        return {"verdict": "SKIPPED", "bytes": 0, "h3": 0, "rec2003": 0,
                "linkedin_hits": 0, "ext_urls": [], "title": ""}, b"", replay2_ok

    from src.core.curl_fetcher import CurlCffiFetcher

    pace()
    _count_request(url)
    try:
        r = CurlCffiFetcher(user_agent=ua).get(url, cookies=cookies or None)
    except Exception as exc:
        print(f"  [{label}] ERROR: {str(exc)[:140]}")
        return {"verdict": "ERROR", "bytes": 0, "h3": 0, "rec2003": 0,
                "linkedin_hits": 0, "ext_urls": [], "title": ""}, b"", replay2_ok

    raw = r.body or b""
    m = classify_body(raw)
    _save_body(m, raw, save_name)
    if (m["verdict"] == "RESULTS" and replay2_ok
            and save_name.startswith("probe_xray_google_replay")
            and save_name != "probe_xray_google_replay2.html"):
        # first result-bearing REPLAY body claims the canonical replay2 name
        (TMP_DIR / "probe_xray_google_replay2.html").write_bytes(raw)
        replay2_ok = False
    if m["verdict"] == "CHALLENGE":
        _LEDGER["challenge_streak"] += 1
        if _LEDGER["challenge_streak"] >= CHALLENGE_WALL:
            _LEDGER["aborted"] = True
    else:
        _LEDGER["challenge_streak"] = 0

    cookies_note = f" cookies[{jar_summary(cookies)}]" if cookies else ""
    print(f"  [{label}] status={r.status} {fmt_metrics(m)}{cookies_note}")
    if m["linkedin_paths"]:
        print(f"      sample: {m['linkedin_paths'][:3]}")
    elif m["ext_urls"]:
        print(f"      sample: {sorted(m['ext_urls'])[:3]}")
    return m, raw, replay2_ok


# --- Q1: replay yesterday's jar ------------------------------------------------------

def run_replay(jar_path: Path) -> dict:
    print(f"=== Q1 — cookie replay (jar: {jar_path.name}) ===")
    if not jar_path.exists():
        print(f"  jar missing: {jar_path} — cannot replay")
        return {"verdict": "ERROR", "h3": 0, "rec2003": 0}
    cookies = load_jar(jar_path)
    age_h = max(0.0, (time.time() - jar_path.stat().st_mtime) / 3600.0)
    print(f"  jar harvested {age_h:.1f}h ago ({jar_path.stat().st_mtime and time.strftime('%Y-%m-%d %H:%M', time.localtime(jar_path.stat().st_mtime))}), {jar_summary(cookies)}")
    url = f"https://www.google.com/search?q={quote_plus(OPERATOR_QUERY)}&hl=en"
    m, _, _ = http_cell("Q1 replay yesterday-jar", url, CHROME_UA, cookies,
                        "probe_xray_google_replay_q1.html", replay2_ok=True)
    return m


# --- Q2: the three never-tried cells --------------------------------------------------

def run_ua_cells(jar_path: Path | None) -> dict:
    print("=== Q2 — never-tried UA / param cells (no cookies unless noted) ===")
    plain_url = f"https://www.google.com/search?q={quote_plus(OPERATOR_QUERY)}"
    gbv_url = f"https://www.google.com/search?q={quote_plus(OPERATOR_QUERY)}&hl=en&gbv=1"
    out: dict[str, dict] = {}
    print("  NOTE (a)/(b): curl_cffi impersonate=chrome keeps TLS at Chrome while the")
    print("  UA header says Firefox/iPhone — mismatch recorded; the cell still answers")
    print("  whether the SERVER serves result/basic HTML to that UA.")
    out["firefox60"] = http_cell("Q2a old-Firefox UA, no cookies", plain_url,
                                 OLD_FIREFOX_UA, None,
                                 "probe_xray_google_ua_firefox60.html", replay2_ok=True)[0]
    out["iphone"] = http_cell("Q2b iPhone Safari UA, no cookies", plain_url,
                              IPHONE_SAFARI_UA, None,
                              "probe_xray_google_ua_iphone.html", replay2_ok=True)[0]
    if jar_path and jar_path.exists():
        cookies = load_jar(jar_path)
        out["cookies_gbv1"] = http_cell("Q2c yesterday-cookies + gbv=1", gbv_url,
                                        CHROME_UA, cookies,
                                        "probe_xray_google_cookies_gbv1.html", replay2_ok=True)[0]
    else:
        print("  [Q2c cookies+gbv=1] SKIPPED — no cookie jar available")
        out["cookies_gbv1"] = {"verdict": "SKIPPED", "h3": 0, "rec2003": 0}
    return out


# --- Q3: refresh procedure (solve -> harvest -> replay-verify) -------------------------

def browser_refresh(url: str) -> dict:
    """One headed-Patchright solve of the operator query; harvest cookies.

    Precedent: scripts/probe_xray_serp.py esc_browser_rung (headless=False per
    ghost.py lesson). Closes the browser in finally. One load; a second goto
    only on a transient navigation error (a served block never raises).
    """
    print(f"=== Q3 — browser refresh (solve -> harvest -> replay) ===")
    try:
        from patchright.sync_api import sync_playwright
    except ImportError as exc:
        print(f"  patchright not importable: {exc}")
        return {"verdict": "ERROR", "cookies": [], "jar_path": None}

    out: dict = {"verdict": "ERROR", "cookies": [], "jar_path": None}
    pw = sync_playwright().start()
    browser = None
    try:
        try:
            browser = pw.chromium.launch(
                headless=False,  # FORCED HEADED — headless is flagged (ghost.py)
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
        except Exception:
            browser = pw.chromium.launch(headless=False, channel="chrome")
        context = browser.new_context(locale="en-US",
                                      viewport={"width": 1440, "height": 900})
        page = context.new_page()

        html = None
        for attempt in (1, 2):
            if _LEDGER["loads"] >= LOAD_BUDGET:
                break
            pace()
            _count_load(url)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
                break
            except Exception as exc:  # transient only — a block never raises
                print(f"  nav error (attempt {attempt}): {str(exc)[:140]}")
        if _LEDGER["loads"] == 0:
            print("  no page load happened (load budget exhausted before start)")
            return out

        deadline = time.monotonic() + SETTLE_S
        while True:
            html = page.content()
            m = classify_body(html.encode("utf-8", "replace"))
            if m["verdict"] in ("RESULTS", "CHALLENGE"):
                break  # results hydrated, or a hard block was served — done waiting
            if time.monotonic() >= deadline:
                break
            time.sleep(2.0)

        (TMP_DIR / "probe_xray_google_refresh_solve.html").write_text(html, encoding="utf-8")
        out["solve_metrics"] = m
        print(f"  [Q3 solve] {fmt_metrics(m)}")

        cookies = [
            c for c in context.cookies()
            if "google.com" in (c.get("domain") or "").lower() and c.get("name")
        ]
        out["cookies"] = cookies
        jar_path = Path(sys.argv[sys.argv.index("--out") + 1]) if "--out" in sys.argv else DEFAULT_JAR
        jar_path.parent.mkdir(parents=True, exist_ok=True)
        jar_path.write_text(json.dumps(cookies, indent=2), encoding="utf-8")
        out["jar_path"] = str(jar_path)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        print(f"  [Q3 harvest] {time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime())}Z — "
              f"{jar_summary(cookies)} -> {jar_path}")
        session_note = ", ".join(
            f"{c['name']}(exp={'session' if c.get('expires', -1) < 0 else time.strftime('%m-%d', time.localtime(c['expires']))})"
            for c in cookies)
        print(f"  [Q3 jar detail] {session_note} (harvested {stamp})")
        try:
            context.close()
        except Exception:
            pass
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        pw.stop()
    return out


def run_refresh() -> dict:
    url = f"https://www.google.com/search?q={quote_plus(OPERATOR_QUERY)}&hl=en"
    solve = browser_refresh(url)
    cookies = [{"name": c["name"], "value": c["value"]} for c in solve.get("cookies", [])]
    if cookies:
        m, _, _ = http_cell("Q3 replay fresh-jar", url, CHROME_UA, cookies,
                            "probe_xray_google_replay_fresh.html", replay2_ok=True)
        solve["replay_metrics"] = m
    else:
        print("  [Q3 replay] SKIPPED — no google cookies harvested")
        solve["replay_metrics"] = {"verdict": "SKIPPED", "h3": 0, "rec2003": 0}
    return solve


# --- offline selftest ------------------------------------------------------------------

def run_selftest() -> int:
    """Classify the known captures (originals + 2026-10-03 live bodies); ZERO
    network. Fails loudly if the classifier disagrees with the verified
    ground truth -- including the captcha-must-not-be-RESULTS regressions."""
    print("=== SELFTEST — offline classification of known captures (no network) ===")
    failures = 0
    for rel, want_verdict, want in SELFTEST_FILES:
        p = REPO_ROOT / rel
        if not p.exists():
            print(f"  {rel}: MISSING (skip)")
            continue
        m = classify_body(p.read_bytes())
        checks = [("verdict", m["verdict"] == want_verdict)]
        checks += [(k, m[k] == v) for k, v in want.items()]
        ok = all(good for _, good in checks)
        failures += 0 if ok else 1
        detail = " ".join(f"{k}={'OK' if good else 'FAIL'}" for k, good in checks)
        print(f"  {Path(rel).name}: {detail}")
        print(f"    {fmt_metrics(m)}")
        if want_verdict == "RESULTS":
            print(f"    rec2003 distinct urls: {len(m['rec_urls'])}; sample: {m['linkedin_paths'][:2]}")
    print(f"SELFTEST: {'PASS' if failures == 0 else f'{failures} FAILURES'}")
    return 1 if failures else 0


# --- entry ------------------------------------------------------------------------------

def print_budget() -> None:
    print(f"budget: http={_LEDGER['http']}/{HTTP_BUDGET} page_loads={_LEDGER['loads']}/{LOAD_BUDGET} "
          f"(challenge streak peak {_LEDGER['challenge_streak']})")
    for k, n in sorted(_LEDGER["per_host"].items()):
        print(f"  {k}: {n}")


def main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    cmd = argv[1] if len(argv) > 1 else "all"
    TMP_DIR.mkdir(exist_ok=True)

    if cmd == "selftest":
        return run_selftest()

    jar = Path(argv[argv.index("--cookies") + 1]) if "--cookies" in argv else YESTERDAY_JAR
    print(f"=== GOOGLE COOKIE REFRESH PIPELINE (ladder T2+3 re-scoped) — {time.strftime('%Y-%m-%dT%H:%M:%S')} ===")
    print(f"operator query: {OPERATOR_QUERY!r}")

    if cmd == "replay":
        run_replay(jar)
    elif cmd == "ua-cells":
        run_ua_cells(jar)
    elif cmd == "refresh":
        run_refresh()
    elif cmd == "all":
        q1 = run_replay(jar)
        print()
        q2 = run_ua_cells(jar)
        print()
        q3 = run_refresh()
        print("\n=== SUMMARY ===")
        print(f"Q1 yesterday-jar replay : {q1.get('verdict')} "
              f"(h3={q1.get('h3')}, 2003rec={q1.get('rec2003')}, linkedin_paths={len(q1.get('linkedin_paths', []))})")
        for k, m in q2.items():
            print(f"Q2 {k:14}: {m.get('verdict')} (h3={m.get('h3')}, 2003rec={m.get('rec2003')})")
        sm = q3.get("solve_metrics", {})
        rm = q3.get("replay_metrics", {})
        print(f"Q3 solve               : {sm.get('verdict')} (h3={sm.get('h3')}, 2003rec={sm.get('rec2003')})")
        print(f"Q3 fresh-jar replay    : {rm.get('verdict')} (h3={rm.get('h3')}, 2003rec={rm.get('rec2003')})")
        print(f"Q3 jar                 : {q3.get('jar_path')}")
    else:
        print(f"unknown command: {cmd}")
        return 2
    print()
    print_budget()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
