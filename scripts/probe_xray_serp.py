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
"""
from __future__ import annotations

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


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    run()
