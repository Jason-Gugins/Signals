"""Task 3 probe spike: which fetch tier retrieves Google/DDG SERP HTML keylessly?

One paced, self-paced reconnaissance pass that GATES the X-ray fetch tier
(plan: .zcode/plans/2026-10-02_182833-xray-serp-prospecting.md, Task 3).
No fetch code wires up until this probe records a verdict.

Matrix:
  engines    google (/search) and ddg (html.duckduckgo.com/html/)
  transports a. plain httpx (honest repo UA — expected dead per prior probes)
             b. curl_cffi chrome-TLS tier (CurlCffiFetcher, Chrome/147 UA — the
                same factory src/identity/ddg_ids.py uses in production)
             c. antibot tier (SignalsTransport; native Rust engine when built,
                else auto-falls back to curl_cffi — both fine, `via` recorded)

Queries: the five operator shapes from the plan (people/hiring/funding/intent).

Anti-ban discipline (template: scripts/probe_wikidata_resolve.py):
  * TOTAL budget: 24 network requests for the whole run (hard stop after).
  * PACE_S >= 3 s sleep between ANY two requests (global — stricter than the
    per-host minimum; each engine's whole ladder hits ONE host anyway).
  * After 2 consecutive hard challenges (consent/captcha/sorry) on one engine,
    STOP probing that engine — record "aborted: challenge wall", move on.
    Never retry a challenge in a loop.
  * A transport that WORKS runs the five queries (5 requests) and SKIPS the
    remaining transports for that engine.

Body-validated, NEVER status-validated: DDG and Google both serve challenge
pages with HTTP 200 (ddg_ids.py docstring + T1 probe). Every body is scanned
for challenge markers in the first 20k chars (case-insensitive) before the
organic-anchor count decides WORKS.

Usage: .venv/Scripts/python.exe scripts/probe_xray_serp.py   (repo root cwd)
Writes: tmp/probe_xray_<engine>.html           first organic SERP body (Task 4 fixture)
        tmp/probe_xray_<engine>_challenge.html first challenge/consent body (if any)
        tmp/probe_xray_<engine>_dead.html      first DEAD body (e.g. a JS-gate shell)
Prints: compact verdict table (engine x transport) + per-host request counts.

Escalation rung (Task 3b, rung 1 triggered the stop condition):
  .venv/Scripts/python.exe scripts/probe_xray_serp.py --escalation
  E1 headed-Patchright browser tier, E2 browser-cookie TLS replay,
  E3 DDG posture variants. Budgets: <=6 page loads, <=8 added HTTP requests.
  --lite-xcheck runs only the lite cross-check follow-up (no browser loads).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote_plus, unquote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TMP_DIR = Path("tmp")

PACE_S = 4.0          # >= 3 s between any two requests (same-host rule satisfied globally)
TOTAL_BUDGET = 24     # hard cap on network requests for the whole run
CHALLENGE_WALL = 2    # consecutive hard challenges on one engine -> abort that engine
SCAN_WINDOW = 20_000  # challenge markers scanned in the first N chars

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)

QUERIES = [
    'site:linkedin.com/in "head of growth" "Austin"',
    'site:linkedin.com/in "we only hire senior"',
    '"we\'re hiring" "head of sales"',
    '"recently funded" "compliance software"',
    'intitle:"head of growth" site:linkedin.com/in',
]

ENGINE_URLS = {
    "google": "https://www.google.com/search?q={q}&num=20&hl=en&filter=0",
    "ddg": "https://html.duckduckgo.com/html/?q={q}",
}

# Google challenge/consent markers (plan Task 3 brief).
GOOGLE_CHALLENGE_MARKERS = (
    "/sorry/",
    "unusual traffic",
    "g-recaptcha",
    "recaptcha",
    "consent.google.com",
)
# DDG markers — EXACT list from src/identity/ddg_ids.py:63-68 (aligned, most
# specific first; the recorded marker is the first hit). ddg_ids.py is the
# single source of truth; "challenge" is deliberately NOT a marker there.
DDG_CHALLENGE_MARKERS = (
    "unfortunately, bots use duckduckgo",
    "anomaly-detected",
    "anomaly",
    "captcha",
)

_EXT_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)
_HOST_RE = re.compile(r"^https?://([A-Za-z0-9.\-]+)", re.I)
_HREF_RE = re.compile(r'href="([^"]+)"', re.I)
_NON_ENGINE_HOSTS = ("google.", "duckduckgo.com", "gstatic.")


def honest_ua() -> str:
    """Honest repo UA for the plain httpx tier (probe_wikidata_resolve.py precedent)."""
    email = os.environ.get("SIGNALS_CONTACT_EMAIL", "").strip()
    if not email:
        env = Path(".env")
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if s.startswith("SIGNALS_CONTACT_EMAIL") and "=" in s:
                    email = s.partition("=")[2].strip()
                    break
    return f"SignalsResearchBot/0.1 (+contact: {email or 'unset'})"


# --- request accounting + pacing ---------------------------------------------

REQUESTS_MADE = 0
PER_HOST: dict[str, int] = {}
_LAST_REQUEST_AT = 0.0


def _host(url: str) -> str:
    from urllib.parse import urlsplit

    return (urlsplit(url).hostname or "?").lower()


def pace() -> None:
    """Sleep so consecutive requests are >= PACE_S apart (global, monotonic)."""
    global _LAST_REQUEST_AT
    wait = PACE_S - (time.monotonic() - _LAST_REQUEST_AT)
    if wait > 0:
        time.sleep(wait)
    _LAST_REQUEST_AT = time.monotonic()


def budget_exhausted() -> bool:
    return REQUESTS_MADE >= TOTAL_BUDGET


# --- transports (each: .name, .fetch(url) -> (status:int, body:bytes)) --------

class HttpxTransport:
    """Baseline plain tier with the honest repo UA (expected dead per probes)."""
    name = "httpx"

    def __init__(self) -> None:
        import httpx

        self._client = httpx.Client(
            headers={"User-Agent": honest_ua()}, timeout=30.0, follow_redirects=True
        )
        self.via = "httpx"

    def fetch(self, url: str) -> tuple[int, bytes]:
        r = self._client.get(url)
        return int(r.status_code), r.content


class CurlCffiTransport:
    """Chrome-TLS tier — same factory the discover flow uses (ddg_ids.py)."""
    name = "curl_cffi"

    def __init__(self) -> None:
        from src.core.curl_fetcher import CurlCffiFetcher

        self._fetcher = CurlCffiFetcher(user_agent=CHROME_UA)
        self.via = "curl_cffi chrome"

    def fetch(self, url: str) -> tuple[int, bytes]:
        r = self._fetcher.get(url)
        return int(r.status), r.body


class SignalsTransportWrap:
    """Antibot tier — native Rust engine when built, else curl_cffi fallback."""
    name = "signals_transport"

    def __init__(self) -> None:
        from src.antibot.python.transport import SignalsTransport

        self._transport = SignalsTransport()
        self.via = "native" if self._transport.engine_available else "curlcffi_fallback"

    def fetch(self, url: str) -> tuple[int, bytes]:
        r = self._transport.fetch(url)
        self.via = r.via
        return int(r.status), r.body


def build_transports() -> list:
    """Instantiate in brief order; a missing dep degrades to a stub cell."""
    out = []
    for cls, missing in ((HttpxTransport, "httpx"), (CurlCffiTransport, "curl_cffi"),
                         (SignalsTransportWrap, "src.antibot.python.transport")):
        try:
            out.append(cls())
        except ImportError:
            out.append(_MissingTransport(cls.name, missing))
        except Exception as exc:  # construction failure — record, don't crash
            out.append(_MissingTransport(cls.name, f"init failed: {exc}"))
    return out


class _MissingTransport:
    def __init__(self, name: str, reason: str) -> None:
        self.name = name
        self.via = f"not installed: {reason}"

    def fetch(self, url: str) -> tuple[int, bytes]:
        raise RuntimeError(self.via)


# --- body validation (pure) ---------------------------------------------------

def challenge_marker(body: str, engine: str) -> str | None:
    """First challenge marker in the first SCAN_WINDOW chars (case-insensitive)."""
    low = (body or "")[:SCAN_WINDOW].casefold()
    markers = GOOGLE_CHALLENGE_MARKERS if engine == "google" else DDG_CHALLENGE_MARKERS
    for marker in markers:
        if marker in low:
            return marker
    return None


def external_anchor_count(body: str) -> int:
    """Count unique external result URLs in href= attrs (probe-level parse:
    unwrap %2F-encoded /url?q= and uddg= wrappers by unquoting first). URLs
    are counted whole, not by host — a site:linkedin.com/in SERP is organic
    even though every result points at linkedin.com."""
    urls: set[str] = set()
    for href in _HREF_RE.findall(body or ""):
        for m in _EXT_URL_RE.finditer(unquote(href)):
            url = m.group(0).rstrip(".,;:")
            hm = _HOST_RE.match(url)
            if not hm:
                continue
            if any(h in hm.group(1).lower() for h in _NON_ENGINE_HOSTS):
                continue
            urls.add(url.lower())
    return len(urls)


def classify(body: bytes, engine: str) -> tuple[str, str]:
    """-> (classification, evidence). Body-validated, never status-validated."""
    text = (body or b"").decode("utf-8", "replace")
    marker = challenge_marker(text, engine)
    if marker:
        return "CHALLENGE", f"marker={marker!r}"
    anchors = external_anchor_count(text)
    if anchors >= 3:
        extra = ""
        if engine == "ddg":
            extra = f" result__a={text.count('result__a')}"
        return "WORKS", f"external_anchors={anchors}{extra}"
    return "DEAD", f"external_anchors={anchors} (no challenge marker)"


# --- probe driver -------------------------------------------------------------

def run() -> None:
    global REQUESTS_MADE
    TMP_DIR.mkdir(exist_ok=True)
    transports = build_transports()
    rows: list[dict] = []  # verdict-table cells

    for engine in ("google", "ddg"):
        challenge_streak = 0
        aborted = False
        saved_organic = False
        saved_challenge = False
        saved_dead = False

        for tr in transports:
            cell = {"engine": engine, "transport": tr.name, "via": tr.via,
                    "status": None, "bytes": None, "classification": "—",
                    "evidence": "", "queries": []}

            if aborted:
                cell["classification"] = "ABORTED"
                cell["evidence"] = "aborted: challenge wall"
                rows.append(cell)
                continue
            if budget_exhausted():
                cell["classification"] = "SKIPPED"
                cell["evidence"] = "budget exhausted"
                rows.append(cell)
                continue

            worked = False
            for qi, query in enumerate(QUERIES, 1):
                if budget_exhausted():
                    cell["evidence"] = (cell["evidence"] + " budget-exhausted").strip()
                    break
                url = ENGINE_URLS[engine].format(q=quote_plus(query))
                pace()
                host = _host(url)
                try:
                    status, body = tr.fetch(url)
                except Exception as exc:
                    cell["queries"].append({"q": qi, "classification": f"ERROR: {exc}"})
                    if qi == 1:
                        cell["classification"] = "ERROR"
                        cell["evidence"] = str(exc)[:120]
                    challenge_streak = 0  # a network error is not a challenge
                    break
                REQUESTS_MADE += 1
                PER_HOST[host] = PER_HOST.get(host, 0) + 1
                klass, evidence = classify(body, engine)
                cell["queries"].append({"q": qi, "status": status, "bytes": len(body),
                                        "classification": klass, "evidence": evidence})
                if qi == 1:
                    cell["status"], cell["bytes"] = status, len(body)
                    cell["classification"], cell["evidence"] = klass, evidence

                if klass == "CHALLENGE":
                    challenge_streak += 1
                    if not saved_challenge:
                        (TMP_DIR / f"probe_xray_{engine}_challenge.html").write_bytes(body)
                        saved_challenge = True
                    if challenge_streak >= CHALLENGE_WALL:
                        aborted = True
                    break  # next transport (never retry a challenge)
                if klass == "DEAD" and not saved_dead:
                    # first dead body per engine is evidence too (e.g. a
                    # 200/JS-gate shell explains WHY without a challenge marker)
                    (TMP_DIR / f"probe_xray_{engine}_dead.html").write_bytes(body)
                    saved_dead = True
                if klass == "WORKS":
                    if not saved_organic:
                        (TMP_DIR / f"probe_xray_{engine}.html").write_bytes(body)
                        saved_organic = True
                    worked = True
                    continue  # next query on THIS transport
                break  # DEAD/ERROR on the probe query -> next transport

            if worked:
                # skip remaining transports for this engine — this one works
                pass
            rows.append(cell)
            if worked:
                for tr2 in transports[transports.index(tr) + 1:]:
                    rows.append({"engine": engine, "transport": tr2.name, "via": tr2.via,
                                 "status": None, "bytes": None,
                                 "classification": "SKIPPED",
                                 "evidence": f"{tr.name} WORKS", "queries": []})
                break

    # --- verdict table --------------------------------------------------------
    print("\n=== X-RAY SERP PROBE VERDICT (body-validated) ===")
    header = f"{'engine':8} {'transport':17} {'via':24} {'status':7} {'bytes':8} {'class':10} evidence"
    print(header)
    print("-" * len(header))
    for c in rows:
        print(f"{c['engine']:8} {c['transport']:17} {str(c['via'])[:24]:24} "
              f"{str(c['status'] or '-'):7} {str(c['bytes'] if c['bytes'] is not None else '-'):8} "
              f"{c['classification']:10} {c['evidence']}")
    per_query = [(c['engine'], c['transport'], q) for c in rows for q in c['queries']]
    print("\nper-query detail:")
    for eng, trn, q in per_query:
        print(f"  {eng}/{trn} q{q['q']}: {q.get('classification')} "
              f"status={q.get('status')} bytes={q.get('bytes')} {q.get('evidence', '')}")
    print(f"\nTOTAL requests: {REQUESTS_MADE} (budget {TOTAL_BUDGET})")
    print("per-host counts:")
    for host, n in sorted(PER_HOST.items()):
        print(f"  {host}: {n}")
    print("samples:", sorted(p.name for p in TMP_DIR.glob("probe_xray_*.html")))


# =============================================================================
# ESCALATION RUNG (Task 3b, 2026-10-02)
#   .venv/Scripts/python.exe scripts/probe_xray_serp.py --escalation
#
# Rung 1 triggered the plan's stop condition: no HTTP transport serves the
# operator queries (google JS-gate shell on every transport; DDG 403/202 on
# operator syntax). This rung tests the plan's named escalation ONLY —
#   E1  browser tier: headed Patchright (the ghost posture; headless gets
#       flagged per ghost.py), one load per engine, cookies harvested.
#   E2  cookie-amortized replay: browser cookies -> CurlCffiFetcher.get(
#       url, cookies=[{name, value}, ...]) — can one browser solve fund later
#       cheap TLS fetches (the solve-and-bounce / RouteState amortization)?
#   E3  DDG posture variants, no browser: lite.duckduckgo.com GET and
#       html.duckduckgo.com POST form, Chrome/147 UA.
# Budgets (hard): <=6 browser page loads, <=8 added HTTP requests. PACE_S
# stays global (also between page loads). Never retry a hard block — one
# retry per engine ONLY for a transient navigation error (a served block
# never raises). Every body classified by markers/anchors, never by status;
# google markers now include the rung-1 discovery /httpservice/retry/enablejs.
# =============================================================================

GOOGLE_HARD_MARKERS = GOOGLE_CHALLENGE_MARKERS
GOOGLE_SOFT_MARKERS = ("/httpservice/retry/enablejs",)  # rung-1 JS-gate discovery
# Bare "anomaly" is a substring -> soft tier for the escalation; the specific
# DDG phrases stay hard (ddg_ids.py list minus the generic "anomaly" entry).
DDG_HARD_MARKERS = ("unfortunately, bots use duckduckgo", "anomaly-detected", "captcha")
DDG_SOFT_MARKERS = ("anomaly",)

BROWSER_LOAD_BUDGET = 6  # goto calls across both engines
HTTP_ESC_BUDGET = 8      # added HTTP requests (E2 replay + E3 posture)
GOTO_TIMEOUT_MS = 40_000
SETTLE_GOOGLE_S = 25.0   # give the JS gate time to resolve into results
SETTLE_DDGS = 12.0       # html endpoint is server-rendered; short settle only

_ESC = {"loads": 0, "http": 0, "per_host": {}}


def _esc_count(url: str, kind: str) -> None:
    """Escalation budget ledger ('load' = browser page load, 'http' = request)."""
    _ESC["loads" if kind == "load" else "http"] += 1
    key = f"{_host(url)} ({kind})"
    _ESC["per_host"][key] = _ESC["per_host"].get(key, 0) + 1


def esc_classify(text: str, engine: str) -> tuple[str, str]:
    """Escalation classifier (body-validated).

    >=3 external anchors = organic WORKS: challenge/block pages link only to
    their own properties, which external_anchor_count filters out, so a block
    page can never reach 3 — anchors are therefore trusted over the soft
    JS-gate marker (whose href vanishes exactly when the gate resolves) and
    any marker found alongside anchors is recorded as a caveat. With <3
    anchors, hard markers decide CHALLENGE, then soft markers, else DEAD.
    """
    low = (text or "")[:SCAN_WINDOW].casefold()
    hard = GOOGLE_HARD_MARKERS if engine == "google" else DDG_HARD_MARKERS
    soft = GOOGLE_SOFT_MARKERS if engine == "google" else DDG_SOFT_MARKERS
    anchors = external_anchor_count(text)
    hit_hard = next((m for m in hard if m in low), None)
    hit_soft = next((m for m in soft if m in low), None)
    if anchors >= 3:
        cav = f" [marker {hit_hard or hit_soft!r} also present]" if (hit_hard or hit_soft) else ""
        return "WORKS", f"external_anchors={anchors}{cav}"
    if hit_hard:
        return "CHALLENGE", f"hard marker={hit_hard!r} external_anchors={anchors}"
    if hit_soft:
        return "CHALLENGE", f"soft marker={hit_soft!r} external_anchors={anchors}"
    return "DEAD", f"external_anchors={anchors} (no marker)"


def esc_browser_load(page, url: str, engine: str) -> tuple[str, str, str]:
    """One goto + settle poll -> (html, klass, evidence). Raises on transient
    errors. Poll watches the DOM resolve: organic anchors or a hard block end
    it; a soft-only JS-gate shell keeps polling until the deadline."""
    _esc_count(url, "load")
    page.goto(url, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
    deadline = time.monotonic() + (SETTLE_GOOGLE_S if engine == "google" else SETTLE_DDGS)
    hard = GOOGLE_HARD_MARKERS if engine == "google" else DDG_HARD_MARKERS
    while True:
        html = page.content()
        klass, ev = esc_classify(html, engine)
        low = html[:SCAN_WINDOW].casefold()
        if klass == "WORKS" or any(m in low for m in hard):
            return html, klass, ev
        if time.monotonic() >= deadline:
            return html, klass, ev  # e.g. a JS-gate shell that never resolved
        time.sleep(2.0)


def esc_browser_rung(urls: dict[str, str]) -> dict[str, dict]:
    """E1: headed Patchright, one load per engine (+1 retry per engine ONLY on
    a transient navigation error). Closes the browser cleanly in finally."""
    out = {e: {"klass": "SKIPPED", "evidence": "", "bytes": None, "loads": 0,
               "cookies": [], "cookie_names": [], "html_path": None} for e in urls}
    try:
        from patchright.sync_api import sync_playwright
    except ImportError as exc:
        for e in out:
            out[e].update(klass="ERROR", evidence=f"patchright not importable: {exc}")
        return out

    pw = sync_playwright().start()
    browser = None
    try:
        try:
            browser = pw.chromium.launch(
                headless=False,  # FORCED HEADED — headless is flagged (ghost.py lesson)
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
        except Exception:
            browser = pw.chromium.launch(headless=False, channel="chrome")
        context = browser.new_context(locale="en-US",
                                      viewport={"width": 1440, "height": 900})
        page = context.new_page()
        for engine, url in urls.items():
            host_key = "google.com" if engine == "google" else "duckduckgo.com"
            cell = out[engine]
            html, klass, ev = None, "ERROR", ""
            loads0 = _ESC["loads"]
            for attempt in (1, 2):
                if _ESC["loads"] >= BROWSER_LOAD_BUDGET:
                    break
                pace()
                try:
                    html, klass, ev = esc_browser_load(page, url, engine)
                    break
                except Exception as exc:  # transient only — a block never raises
                    klass, ev = "ERROR", f"nav error (attempt {attempt}): {str(exc)[:140]}"
            cell.update(klass=klass, evidence=ev, loads=_ESC["loads"] - loads0,
                        bytes=len(html.encode("utf-8", "replace")) if html else None)
            if html is not None:
                p = TMP_DIR / f"probe_xray_{engine}_browser.html"
                p.write_text(html, encoding="utf-8")
                cell["html_path"] = str(p)
            try:
                mine = [c for c in context.cookies()
                        if host_key in (c.get("domain") or "").lower()]
            except Exception:
                mine = []
            cell["cookies"] = mine
            cell["cookie_names"] = sorted({c.get("name", "") for c in mine})
            (TMP_DIR / f"probe_xray_{engine}_browser_cookies.json").write_text(
                json.dumps([{k: c.get(k) for k in ("name", "value", "domain")}
                            for c in mine], indent=2), encoding="utf-8")
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


def esc_cookie_replay(engine: str, url: str, browser_cookies: list[dict]) -> dict:
    """E2: replay the SAME operator URL through the TLS tier with the
    browser-harvested cookies (host-filtered, name+value only)."""
    host_key = "google.com" if engine == "google" else "duckduckgo.com"
    mine = [{"name": c["name"], "value": c["value"]} for c in browser_cookies or []
            if host_key in (c.get("domain") or "").lower() and c.get("name")]
    cell = {"klass": "SKIPPED", "evidence": "", "status": None, "bytes": None,
            "cookie_names": sorted({c["name"] for c in mine})}
    if not mine:
        cell["evidence"] = "no host cookies harvested in E1"
        return cell
    if _ESC["http"] >= HTTP_ESC_BUDGET:
        cell["evidence"] = "escalation HTTP budget exhausted"
        return cell
    from src.core.curl_fetcher import CurlCffiFetcher
    pace()
    _esc_count(url, "http")
    try:
        r = CurlCffiFetcher(user_agent=CHROME_UA).get(url, cookies=mine)
    except Exception as exc:
        cell.update(klass="ERROR", evidence=f"replay error: {str(exc)[:140]}")
        return cell
    body = r.body or b""
    (TMP_DIR / f"probe_xray_{engine}_replay.html").write_bytes(body)
    klass, ev = esc_classify(body.decode("utf-8", "replace"), engine)
    cell.update(status=int(r.status), bytes=len(body), klass=klass,
                evidence=f"{ev} | replayed: {', '.join(cell['cookie_names'])}")
    return cell


def _lite_get(qid: str, query: str) -> dict:
    """One paced GET on lite.duckduckgo.com/lite/ (curl_cffi, Chrome/147)."""
    cell = {"klass": "SKIPPED", "evidence": "", "status": None, "bytes": None}
    if _ESC["http"] >= HTTP_ESC_BUDGET:
        cell["evidence"] = "escalation HTTP budget exhausted"
        return cell
    from src.core.curl_fetcher import CurlCffiFetcher
    url = f"https://lite.duckduckgo.com/lite/?q={quote_plus(query)}"
    pace()
    _esc_count(url, "http")
    try:
        r = CurlCffiFetcher(user_agent=CHROME_UA).get(url)
    except Exception as exc:
        cell.update(klass="ERROR", evidence=f"error: {str(exc)[:140]}")
        return cell
    body = r.body or b""
    (TMP_DIR / f"probe_xray_ddg_lite_{qid}.html").write_bytes(body)
    klass, ev = esc_classify(body.decode("utf-8", "replace"), "ddg")
    cell.update(status=int(r.status), bytes=len(body), klass=klass, evidence=ev)
    return cell


def esc_posture_rung(q1: str) -> dict[str, dict]:
    """E3: cheap DDG posture variants for the operator query, no browser."""
    out: dict[str, dict] = {}
    # (a) lite endpoint, GET
    out["ddg_lite_get"] = _lite_get("q1", q1)
    # (b) html endpoint, POST form body
    cell = {"klass": "SKIPPED", "evidence": "", "status": None, "bytes": None}
    if _ESC["http"] < HTTP_ESC_BUDGET:
        pace()
        _esc_count("https://html.duckduckgo.com/html/", "http")
        try:
            from curl_cffi import requests as curl_requests
            r = curl_requests.post("https://html.duckduckgo.com/html/",
                                   data={"q": q1}, impersonate="chrome",
                                   headers={"User-Agent": CHROME_UA}, timeout=30)
            body = r.content or b""
            (TMP_DIR / "probe_xray_ddg_post.html").write_bytes(body)
            klass, ev = esc_classify(body.decode("utf-8", "replace"), "ddg")
            cell.update(status=int(r.status_code), bytes=len(body), klass=klass, evidence=ev)
        except Exception as exc:
            cell.update(klass="ERROR", evidence=f"error: {str(exc)[:140]}")
    else:
        cell["evidence"] = "escalation HTTP budget exhausted"
    out["ddg_html_post"] = cell
    return out


LITE_XCHECK_DEFAULT = (  # (qid, query): rung-1 failure shapes + controls on lite
    ("q5", QUERIES[4]),                 # intitle: shape  (html-endpoint 403 shape)
    ("q3", QUERIES[2]),                 # quote-pair      (html-endpoint 202 shape)
    ("q2", QUERIES[1]),                 # site: family sibling of q1
    ("ctrl_plain", "stripe official website"),  # shape-gate vs velocity-gate probe
)


def esc_lite_crosscheck(specs=None) -> list[tuple[str, dict]]:
    """Follow-up to a GO on lite: which query shapes does lite actually serve?
    Paced GETs, no browser; NEVER re-requests a challenge already observed."""
    chosen = list(specs) if specs else list(LITE_XCHECK_DEFAULT)
    return [(qid, _lite_get(qid, query)) for qid, query in chosen]


def run_lite_xcheck(only: list[str] | None = None) -> None:
    """Standalone follow-up (--lite-xcheck [qid ...]): lite cross-check cells
    only, for use after an --escalation run that already spent its budget."""
    TMP_DIR.mkdir(exist_ok=True)
    specs = None
    if only:
        valid = {qid for qid, _ in LITE_XCHECK_DEFAULT}
        bad = [q for q in only if q not in valid]
        if bad:
            raise SystemExit(f"unknown lite xcheck ids: {bad}; valid: {sorted(valid)}")
        specs = [(qid, q) for qid, q in LITE_XCHECK_DEFAULT if qid in only]
    print("=== X-RAY SERP PROBE — LITE CROSS-CHECK (Task 3b follow-up) ===")
    rows = esc_lite_crosscheck(specs)
    for qid, c in rows:
        print(f"  lite {qid}: status={str(c.get('status') or '-'):5} "
              f"bytes={str(c.get('bytes') if c.get('bytes') is not None else '-'):8} "
              f"{c['klass']:10} {c['evidence']}")
    print(f"budget: page_loads={_ESC['loads']}/{BROWSER_LOAD_BUDGET}  "
          f"http={_ESC['http']}/{HTTP_ESC_BUDGET}")
    for k, n in sorted(_ESC["per_host"].items()):
        print(f"  {k}: {n}")


def run_escalation() -> None:
    """Task 3b escalation rung: E1 browser tier -> E2 cookie replay -> E3 posture."""
    TMP_DIR.mkdir(exist_ok=True)
    q1 = QUERIES[0]
    urls = {e: ENGINE_URLS[e].format(q=quote_plus(q1)) for e in ("google", "ddg")}
    print("=== X-RAY SERP PROBE — ESCALATION RUNG (Task 3b, 2026-10-02) ===")
    print(f"probe query (rung-1 q1): {q1!r}\n")

    print("[E1] headed browser tier (patchright, headless=False) ...")
    browser_cells = esc_browser_rung(urls)
    print("[E2] cookie-amortized TLS replay (CurlCffiFetcher + browser cookies) ...")
    replay_cells = {e: esc_cookie_replay(e, urls[e], browser_cells[e]["cookies"])
                    for e in ("google", "ddg")}
    print("[E3] DDG posture variants (no browser) ...")
    posture_cells = esc_posture_rung(q1)
    cross_cells: list[tuple[str, dict]] = []
    if posture_cells["ddg_lite_get"]["klass"] == "WORKS":
        print("[E3+] lite GO cross-check (q5 intitle shape, q3 quote-pair shape) ...")
        cross_cells = esc_lite_crosscheck()

    def row(label: str, c: dict) -> str:
        return (f"  {label:26} status={str(c.get('status') or '-'):5} "
                f"bytes={str(c.get('bytes') if c.get('bytes') is not None else '-'):8} "
                f"{c['klass']:10} {c['evidence']}")

    print("\n--- escalation verdict table (body-validated) ---")
    for e in ("google", "ddg"):
        c = browser_cells[e]
        print(f"  {'E1 browser ' + e:26} loads={c['loads']} "
              f"bytes={str(c['bytes'] if c['bytes'] is not None else '-'):8} "
              f"{c['klass']:10} {c['evidence']}")
        if c["cookie_names"]:
            print(f"  {'':26} cookies: {', '.join(c['cookie_names'])}")
    for e in ("google", "ddg"):
        print(row(f"E2 replay {e}", replay_cells[e]))
    for label, c in posture_cells.items():
        print(row(f"E3 {label}", c))
    for qid, c in cross_cells:
        print(row(f"E3+ lite {qid}", c))

    winners = ([f"E1/{e}" for e in ("google", "ddg") if browser_cells[e]["klass"] == "WORKS"]
               + [f"E2/{e}" for e in ("google", "ddg") if replay_cells[e]["klass"] == "WORKS"]
               + [f"E3/{lbl.split('_')[-1]}" for lbl, c in posture_cells.items()
                  if c["klass"] == "WORKS"]
               + [f"E3+/{qid}" for qid, c in cross_cells if c["klass"] == "WORKS"])
    print(f"\nORGANIC PATHS: {winners if winners else 'NONE — no escalation path served an operator SERP'}")
    print(f"budget: page_loads={_ESC['loads']}/{BROWSER_LOAD_BUDGET}  "
          f"http={_ESC['http']}/{HTTP_ESC_BUDGET}")
    for k, n in sorted(_ESC["per_host"].items()):
        print(f"  {k}: {n}")
    print("samples:", sorted(p.name for p in TMP_DIR.glob("probe_xray_*_browser*"))
          + sorted(p.name for p in TMP_DIR.glob("probe_xray_*_replay.html"))
          + sorted(p.name for p in TMP_DIR.glob("probe_xray_ddg_lite*.html"))
          + sorted(p.name for p in TMP_DIR.glob("probe_xray_ddg_post.html")))


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if "--escalation" in sys.argv[1:]:
        run_escalation()
    elif "--lite-xcheck" in sys.argv[1:]:
        idx = sys.argv.index("--lite-xcheck")
        run_lite_xcheck(sys.argv[idx + 1:] or None)
    else:
        run()
