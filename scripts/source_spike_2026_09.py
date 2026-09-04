#!/usr/bin/env python3
"""P3 Task 14 spike: paced live probes of three candidate data sources.

Candidates (plan 2026-09-04_160212-sources-waterfall-roadmap.md, Task 14):
  1. Trustpilot reviews        — https://www.trustpilot.com/review/<domain>
  2. Gartner-network reviews   — softwareadvice.com + getapp.com (shared corpus)
  3. usaspending.gov contracts — keyless JSON API at api.usaspending.gov

Question per candidate: does a plain scripted GET return server-rendered
content, an anti-bot challenge, or a JS shell? Verdicts (GO plain-fetch /
browser-tier / STUB fixture-only / NO-GO / INCONCLUSIVE-network) scope the
future build batch so it never needs to re-probe. Mirrors the P2 spike
(scripts/p2_source_spike.py, data/probe/P2_SOURCE_SPIKE.md).

Anti-bot / etiquette discipline (HARD constraints):
  * <= 6 requests per host, counted by the Pacer (every attempt counts).
  * >= 5.0 s sleep between ANY two requests in the whole run (global pacer).
  * 15 s timeout per request; honest, polite User-Agent.
  * Public pages only. No auth, no cookies, no challenge-bypass: if a
    challenge appears, ONE same-URL second look via curl_cffi (chrome TLS
    fingerprint, the repo's standard fetcher) is allowed to tell a passive
    fingerprint block apart from a hard JS challenge — then the host stops.
  * Network-unreachable host: at most 2 attempts, then INCONCLUSIVE-network.
  * No personal data collected: doc/JSON evidence quotes structural markers
    only; saved HTML dumps are truncated to their first 64 KiB (structure and
    challenge markers live at the top of documents).

Usage:
    ./.venv/Scripts/python.exe scripts/source_spike_2026_09.py            # all hosts
    ./.venv/Scripts/python.exe scripts/source_spike_2026_09.py --host trustpilot
    ./.venv/Scripts/python.exe scripts/source_spike_2026_09.py --host softwareadvice --host getapp

Writes: data/probe/p3_spike_<host>_<n>_<method>.<ext> (truncated raw dumps) +
        data/probe/p3_source_spike_2026_09_findings.json (merged per host).
"""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

OUT_DIR = Path("data/probe")
OUT_DIR.mkdir(parents=True, exist_ok=True)

MAX_PER_HOST = 6
PACE_S = 5.0
TIMEOUT_S = 15.0
MAX_ATTEMPTS_UNREACHABLE = 2

UA = "signals-source-spike/1.0 (paced local research probe; >=5s between requests)"
BASE_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# Hard JS-challenge markers (a body containing one of these needs a real
# browser; that IS the verdict — never attempt to defeat it).
JS_CHALLENGE_MARKERS = (
    "just a moment",
    "cf_chl_opt",
    "__cf_chl",
    "cf-browser-verification",
    "challenge-platform",
    "checking your browser",
    "verify you are a human",
    "verifying your connection",
    "verifying connection",
    "awswaf",
    "datadome",
    "captcha-delivery",
    "px-captcha",
    "incapsula",
)
HARD_BLOCK_STATUSES = {403, 429, 503}
BLOCKED_CLASSES = {"challenge-js", "http-403", "http-429", "http-503"}

REVIEW_MARKERS: dict[str, list[str]] = {
    # structural markers only — counts and tag/key names, never personal data
    "trustpilot": [
        r'"@type"\s*:\s*"Review"',
        r'"@type"\s*:\s*"Product"',
        r'itemprop="reviewBody"',
        r"<article\b",
        r"businessUnit",
        r'"numberOfReviews"',
        r"__NEXT_DATA__",
        r"data-review-id",
        r"consumerReviewCount",
    ],
    "softwareadvice": [
        r'"@type"\s*:\s*"Review"',
        r'"aggregateRating"',
        r'href="/((?:[a-z0-9-]+)/(?:[a-z0-9-]+-profile))/"',
        r'class="[^"]*review',
        r"__NEXT_DATA__",
        r'"reviewCount"',
    ],
    "getapp": [
        r'"@type"\s*:\s*"Review"',
        r'"aggregateRating"',
        r'class="[^"]*review',
        r"__NEXT_DATA__",
        r'"reviewCount"',
        r'href="/((?:[a-z0-9-]+)/(?:[a-z0-9-]+))/"',
    ],
    "usaspending": [r'"recipient_id"', r'"results"', r'"Award ID"', r'"detail"'],
}


# ---------------------------------------------------------------- pure helpers
def looks_like_challenge(text: str) -> tuple[bool, str]:
    """(is_js_challenge, marker) over the head of the body. PURE."""
    low = text[:8000].lower()
    for marker in JS_CHALLENGE_MARKERS:
        if marker in low:
            return True, marker
    return False, ""


def classify(status: int, text: str, content_type: str) -> str:
    """Response classification. PURE."""
    challenge, _ = looks_like_challenge(text)
    if challenge:
        return "challenge-js"
    if status == 404:
        return "404"
    if status in HARD_BLOCK_STATUSES:
        return f"http-{status}"  # hard block without a JS challenge body
    if status != 200:
        return f"http-{status}"
    if "json" in content_type.lower() or text.lstrip()[:1] in "{[":
        return "server-rendered-json"
    no_scripts = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.S | re.I)
    if "<script" in text and len(no_scripts.strip()) < 3000:
        return "client-rendered-js-shell"
    return "server-rendered-html"


def ldjson_types(text: str) -> list[str]:
    """@type values embedded in JSON-LD blocks (structure only). PURE."""
    types: list[str] = []
    for blob in re.findall(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        text,
        flags=re.S | re.I,
    ):
        try:
            data = json.loads(blob)
        except Exception:  # noqa: BLE001 — malformed ld+json is fine to skip
            continue
        for node in data if isinstance(data, list) else [data]:
            if isinstance(node, dict) and "@type" in node:
                t = node["@type"]
                types.append(t if isinstance(t, str) else ",".join(map(str, t)))
    return types


def marker_hits(text: str, patterns: list[str]) -> dict[str, int]:
    """First-100KB hit counts per structural marker. PURE."""
    head = text[:100_000]
    return {p: len(re.findall(p, head)) for p in patterns}


def snip_matches(text: str, pattern: str, n: int = 5) -> list[str]:
    """First n group(1) captures of pattern over the FULL body. PURE."""
    out: list[str] = []
    for m in re.finditer(pattern, text):
        out.append(m.group(1) if m.groups else m.group(0))
        if len(out) >= n:
            break
    return out


# ------------------------------------------------------------------- transport
def _httpx_request(url: str, *, method: str, json_body: dict | None):
    import httpx

    with httpx.Client(follow_redirects=True, timeout=TIMEOUT_S) as client:
        r = client.request(method, url, json=json_body, headers=BASE_HEADERS)
        chain = [
            {"status": h.status_code, "location": str(h.headers.get("location", ""))}
            for h in r.history
        ]
        return {
            "status": r.status_code,
            "final_url": str(r.url),
            "content_type": r.headers.get("content-type", ""),
            "headers": {
                k: r.headers.get(k, "")
                for k in ("server", "cf-ray", "cf-mitigated", "x-cache", "allow")
            },
            "redirect_chain": chain,
            "text": r.text,
        }


def _curl_request(url: str, *, method: str, json_body: dict | None):
    """Second-look fingerprint check: same honest UA, chrome TLS fingerprint."""
    from curl_cffi import requests as curl_requests

    r = curl_requests.request(
        method,
        url,
        json=json_body,
        headers=BASE_HEADERS,
        timeout=TIMEOUT_S,
        impersonate="chrome",
        allow_redirects=True,
    )
    return {
        "status": r.status_code,
        "final_url": str(r.url),
        "content_type": r.headers.get("content-type", ""),
        "headers": {
            k: r.headers.get(k, "")
            for k in ("server", "cf-ray", "cf-mitigated", "x-cache", "allow")
        },
        "redirect_chain": [],
        "text": r.text,
    }


class Pacer:
    """Global pacer + per-host budget counter. Every attempt is counted."""

    def __init__(self) -> None:
        self._last_t = 0.0
        self.host_counts: dict[str, int] = {}

    def request(
        self,
        host: str,
        url: str,
        *,
        method: str = "GET",
        json_body: dict | None = None,
        use_curl: bool = False,
        note: str = "",
        markers: list[str] | None = None,
        extract: dict[str, str] | None = None,
        save_ext: str = "txt",
    ) -> dict:
        n = self.host_counts.get(host, 0)
        if n >= MAX_PER_HOST:
            print(f"  [{host}] BUDGET EXHAUSTED ({MAX_PER_HOST}) — skipping {url}", flush=True)
            return {"host": host, "url": url, "skipped": "budget-exhausted"}
        rec: dict = {"host": host, "url": url, "method": method, "note": note,
                     "client": "curl_cffi" if use_curl else "httpx"}
        attempts = 0
        while True:
            attempts += 1
            if self._last_t:
                wait = PACE_S - (time.monotonic() - self._last_t)
                if wait > 0:
                    print(f"  [pace] sleeping {wait:.1f}s", flush=True)
                    time.sleep(wait)
            self.host_counts[host] = n + attempts
            self._last_t = time.monotonic()
            print(f"  [{host}] request {n + attempts}/{MAX_PER_HOST} {method} {url}"
                  f"{' (curl second look)' if use_curl else ''}", flush=True)
            t0 = time.monotonic()
            try:
                raw = (_curl_request if use_curl else _httpx_request)(
                    url, method=method, json_body=json_body
                )
                break
            except Exception as exc:  # noqa: BLE001 — probe must never crash the run
                rec["error"] = f"{type(exc).__name__}: {exc}"
                if attempts < MAX_ATTEMPTS_UNREACHABLE:
                    print(f"    attempt failed ({rec['error']}); one paced retry", flush=True)
                    continue
                rec["unreachable"] = True
                print(f"    UNREACHABLE after {attempts} attempts", flush=True)
                return rec
        rec["elapsed_s"] = round(time.monotonic() - t0, 2)
        rec["status"] = raw["status"]
        rec["final_url"] = raw["final_url"]
        rec["content_type"] = raw["content_type"]
        rec["response_headers"] = raw["headers"]
        rec["redirect_chain"] = raw["redirect_chain"]
        rec["bytes"] = len(raw["text"])
        rec["classification"] = classify(raw["status"], raw["text"], raw["content_type"])
        chal, chal_marker = looks_like_challenge(raw["text"])
        rec["challenge"] = chal
        rec["challenge_marker"] = chal_marker
        rec["ldjson_types"] = ldjson_types(raw["text"]) if "html" in raw["content_type"] else []
        if markers:
            rec["marker_hits"] = marker_hits(raw["text"], markers)
        if extract:
            # full-text URL-shape discovery (dumps are capped at 64 KiB)
            rec["extracts"] = {
                field: snip_matches(raw["text"], pat)
                for field, pat in extract.items()
            }
        # truncated structural dump (first 64 KiB) — local, gitignored
        label = re.sub(r"[^a-z0-9]+", "-", f"{host}-{n + attempts}-{method.lower()}").strip("-")
        dump = OUT_DIR / f"p3_spike_{label}.{save_ext}"
        dump.write_text(raw["text"][:65536], encoding="utf-8")
        rec["saved"] = str(dump)
        print(f"    -> status={rec['status']} bytes={rec['bytes']} {rec['classification']}"
              f"{' CHALLENGE:' + chal_marker if chal else ''}", flush=True)
        return rec


def _dump_head(rec: dict) -> str:
    """Saved 64-KiB dump text for in-memory discovery steps."""
    try:
        return Path(rec["saved"]).read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""


def _blocked(rec: dict) -> bool:
    """True when the response is a challenge or a hard block (403/429/503)."""
    return rec.get("classification") in BLOCKED_CLASSES or bool(rec.get("challenge"))


def _second_look_verdict(r_curl: dict) -> str:
    """Verdict after the one allowed curl_cffi second look. PURE."""
    cls = r_curl.get("classification", "")
    if cls == "server-rendered-html" or cls == "server-rendered-json":
        return "GO plain-fetch via curl_cffi chrome fingerprint (plain httpx blocked)"
    if r_curl.get("challenge") or cls == "challenge-js":
        return "browser-tier (JS challenge on both clients; real-browser pass unprobed)"
    if cls == "client-rendered-js-shell":
        return "browser-tier (JS shell, no SSR content)"
    return "browser-tier"


# ---------------------------------------------------------------- probe flows
def probe_trustpilot(p: Pacer) -> dict:
    host = "www.trustpilot.com"
    markers = REVIEW_MARKERS["trustpilot"]
    f: dict = {"name": "Trustpilot reviews", "host": host, "requests": []}

    r1 = p.request(host, "https://www.trustpilot.com/review/www.microsoft.com",
                   note="well-known company domain", markers=markers, save_ext="html")
    f["requests"].append(r1)
    if r1.get("unreachable"):
        f["verdict"] = "INCONCLUSIVE-network"
        return f

    if r1.get("classification") == "404":
        # maybe the <domain> shape is wrong — one alternate profile
        r2 = p.request(host, "https://www.trustpilot.com/review/www.zendesk.com",
                       note="alternate domain after 404", markers=markers, save_ext="html")
        f["requests"].append(r2)
        if r2.get("unreachable"):
            f["verdict"] = "INCONCLUSIVE-network"
            return f
        f["url_shape"] = f"404 on microsoft profile; alternate final URL: {r2.get('final_url')}"
        if r2.get("classification") == "server-rendered-html":
            f["verdict"] = "GO plain-fetch"
        elif _blocked(r2):
            r3 = p.request(host, r2["url"], use_curl=True,
                           note="fingerprint second look after block",
                           markers=markers, save_ext="html")
            f["requests"].append(r3)
            f["verdict"] = _second_look_verdict(r3)
        elif r2.get("classification") == "client-rendered-js-shell":
            f["verdict"] = "browser-tier (JS shell, no SSR content)"
        else:
            f["verdict"] = "STUB fixture-only"
        return f

    cls = r1.get("classification", "")
    if cls == "server-rendered-html":
        r3 = p.request(host, "https://www.trustpilot.com/review/www.zendesk.com",
                       note="B2B-relevant profile shape check", markers=markers, save_ext="html")
        f["requests"].append(r3)
        f["url_shape"] = f"https://www.trustpilot.com/review/<domain> (canonical: {r1.get('final_url')})"
        f["verdict"] = "GO plain-fetch"
        if r3.get("classification") != "server-rendered-html":
            f["verdict"] += f" (zendesk profile: {r3.get('classification', 'error')})"
    elif _blocked(r1):
        r2 = p.request(host, r1["url"], use_curl=True,
                       note="fingerprint second look after block", markers=markers, save_ext="html")
        f["requests"].append(r2)
        f["verdict"] = _second_look_verdict(r2)
    elif cls == "client-rendered-js-shell":
        f["verdict"] = "browser-tier (JS shell, no SSR content)"
    else:
        f["verdict"] = "STUB fixture-only"
    return f


def probe_softwareadvice(p: Pacer) -> dict:
    host = "www.softwareadvice.com"
    markers = REVIEW_MARKERS["softwareadvice"]
    extracts = {"profile_links": r'href="/([a-z0-9-]+/[a-z0-9-]+-profile)/"'}
    f: dict = {"name": "Software Advice reviews (Gartner network)", "host": host, "requests": []}

    r1 = p.request(host, "https://www.softwareadvice.com/crm/",
                   note="category index — URL-shape discovery", markers=markers,
                   extract=extracts, save_ext="html")
    f["requests"].append(r1)
    if r1.get("unreachable"):
        f["verdict"] = "INCONCLUSIVE-network"
        return f
    pref_curl = False
    base = r1
    if _blocked(r1):
        r2 = p.request(host, r1["url"], use_curl=True,
                       note="fingerprint second look after block", markers=markers,
                       extract=extracts, save_ext="html")
        f["requests"].append(r2)
        if r2.get("unreachable"):
            f["verdict"] = "INCONCLUSIVE-network"
            return f
        if r2.get("classification") != "server-rendered-html":
            f["verdict"] = _second_look_verdict(r2)
            return f
        base, pref_curl = r2, True  # repo's standard fetcher passes; keep using it

    links = base.get("extracts", {}).get("profile_links") or []
    if links:
        url2, how = f"https://www.softwareadvice.com/{links[0]}/", f"discovered on category page: /{links[0]}/"
    else:
        url2, how = "https://www.softwareadvice.com/hr/bamboohr-profile/", "fixed fallback guess (no -profile link found)"
    r3 = p.request(host, url2, use_curl=pref_curl, note=f"product page ({how})",
                   markers=markers, save_ext="html")
    f["requests"].append(r3)
    if r3.get("unreachable"):
        f["verdict"] = "INCONCLUSIVE-network"
        return f
    f["url_shape"] = f"{url2} ({how})"
    if r3.get("classification") == "server-rendered-html":
        f["verdict"] = "GO plain-fetch"
        if pref_curl:
            f["verdict"] += " via curl_cffi chrome fingerprint (plain httpx CF-blocked)"
    elif _blocked(r3):
        if pref_curl:
            f["verdict"] = "browser-tier (curl_cffi also blocked on the product page)"
        else:
            r4 = p.request(host, url2, use_curl=True,
                           note="fingerprint second look after block", markers=markers, save_ext="html")
            f["requests"].append(r4)
            f["verdict"] = _second_look_verdict(r4)
    elif r3.get("classification") == "client-rendered-js-shell":
        f["verdict"] = "browser-tier (JS shell, no SSR content)"
    else:
        f["verdict"] = "STUB fixture-only"
    return f


def probe_getapp(p: Pacer) -> dict:
    host = "www.getapp.com"
    markers = REVIEW_MARKERS["getapp"]
    extracts = {"product_links": r'href="/([a-z0-9-]+/a/[a-z0-9-]+)/"'}
    f: dict = {"name": "GetApp reviews (Gartner network)", "host": host, "requests": []}

    r1 = p.request(host, "https://www.getapp.com/",
                   note="homepage — URL-shape discovery + redirect check", markers=markers,
                   extract=extracts, save_ext="html")
    f["requests"].append(r1)
    if r1.get("unreachable"):
        f["verdict"] = "INCONCLUSIVE-network"
        return f
    if r1.get("redirect_chain"):
        f["redirect_observation"] = r1["redirect_chain"] + [{"final_url": r1.get("final_url")}]
    pref_curl = False
    base = r1
    if _blocked(r1):
        r2 = p.request(host, r1["url"], use_curl=True,
                       note="fingerprint second look after block", markers=markers,
                       extract=extracts, save_ext="html")
        f["requests"].append(r2)
        if r2.get("unreachable"):
            f["verdict"] = "INCONCLUSIVE-network"
            return f
        if r2.get("classification") != "server-rendered-html":
            f["verdict"] = _second_look_verdict(r2)
            return f
        base, pref_curl = r2, True  # repo's standard fetcher passes; keep using it

    links = base.get("extracts", {}).get("product_links") or []
    if links:
        url2, how = f"https://www.getapp.com/{links[0]}/", f"discovered product link on homepage: /{links[0]}/"
    else:
        url2 = "https://www.getapp.com/customer-management-software/a/salesforce/"
        how = "fixed fallback guess (no product link found)"
    r3 = p.request(host, url2, use_curl=pref_curl, note=f"product page ({how})",
                   markers=markers, save_ext="html")
    f["requests"].append(r3)
    if r3.get("unreachable"):
        f["verdict"] = "INCONCLUSIVE-network"
        return f
    f["url_shape"] = f"{url2} ({how})"
    if r3.get("classification") == "server-rendered-html" and not r3.get("challenge"):
        f["verdict"] = "GO plain-fetch"
        if pref_curl:
            f["verdict"] += " via curl_cffi chrome fingerprint (plain httpx CF-blocked)"
    elif _blocked(r3):
        if pref_curl:
            f["verdict"] = "browser-tier (curl_cffi also blocked on the product page)"
        else:
            r4 = p.request(host, url2, use_curl=True,
                           note="fingerprint second look after block", markers=markers, save_ext="html")
            f["requests"].append(r4)
            f["verdict"] = _second_look_verdict(r4)
    elif r3.get("classification") == "client-rendered-js-shell":
        f["verdict"] = "browser-tier (JS shell, no SSR content)"
    else:
        f["verdict"] = "STUB fixture-only"
    return f


def probe_usaspending(p: Pacer) -> dict:
    host = "api.usaspending.gov"
    markers = REVIEW_MARKERS["usaspending"]
    API = "https://api.usaspending.gov"
    KEYWORD = "booz allen hamilton"
    f: dict = {"name": "usaspending.gov federal contracts (keyless JSON API)",
               "host": host, "requests": []}

    # 1. method discovery: a 405's body/Allow header documents the verb
    r1 = p.request(host, f"{API}/api/v2/recipient/", method="GET",
                   note="recipient keyword GET (method/shape discovery)",
                   markers=markers, save_ext="json")
    f["requests"].append(r1)

    # 2. the 405 says POST-only — recipient resolver with hash ids
    r2 = p.request(host, f"{API}/api/v2/recipient/",
                   method="POST", json_body={"keyword": KEYWORD, "limit": 10},
                   note="recipient resolver (POST, per the 405)", markers=markers, save_ext="json")
    f["requests"].append(r2)
    recipient_id = ""
    if r2.get("status") == 200:
        try:
            blob = json.loads(_dump_head(r2))
            results = blob.get("results") or []
            if results:
                # verified shape: results[].id is the recipient hash ("...-C"/"...-P")
                recipient_id = results[0].get("id", "") or results[0].get("recipient_id", "")
                f["recipient_resolver_first_result_keys"] = sorted(results[0].keys())
        except Exception as exc:  # noqa: BLE001
            f["recipient_resolver_parse_note"] = repr(exc)

    # 3. name-level fuzzy autocomplete (returns names only, no hash ids)
    r3 = p.request(host, f"{API}/api/v2/autocomplete/recipient/",
                   method="POST", json_body={"search_text": KEYWORD, "limit": 10},
                   note="recipient autocomplete (POST)", markers=markers, save_ext="json")
    f["requests"].append(r3)

    # 4. empty-filters probe: a 4xx error body documents the required schema
    r4 = p.request(host, f"{API}/api/v2/search/spending_by_award/",
                   method="POST", json_body={"filters": {}},
                   note="empty filters — 4xx error body documents the schema",
                   markers=markers, save_ext="json")
    f["requests"].append(r4)

    body = {
        "filters": {
            "award_type_codes": ["A", "B", "C", "D"],
            "time_period": [{"start_date": "2025-09-01", "end_date": "2026-09-01"}],
            "keywords": [KEYWORD],
        },
        "fields": ["Award ID", "Recipient Name", "Start Date", "End Date",
                   "Award Amount", "Awarding Agency"],
        "page": 1,
        "limit": 10,
        "sort": "Award Amount",
        "order": "desc",
        "subawards": False,
    }
    r5 = p.request(host, f"{API}/api/v2/search/spending_by_award/",
                   method="POST", json_body=body,
                   note="contract search by keyword", markers=markers, save_ext="json")
    f["requests"].append(r5)

    if r5.get("status") in (400, 422) and p.host_counts.get(host, 0) < MAX_PER_HOST:
        # one deterministic corrective retry when the error body names the problem
        err_head = _dump_head(r5)[:2000].lower()
        if any(w in err_head for w in ("time_period", "date", "keyword")):
            if "time_period" in err_head or "date" in err_head:
                body["filters"]["time_period"] = [{"start_date": "2026-01-01", "end_date": "2026-09-01"}]
            if "keyword" in err_head:
                body["filters"].pop("keywords", None)
            r5b = p.request(host, f"{API}/api/v2/search/spending_by_award/",
                            method="POST", json_body=body,
                            note="corrective retry per 4xx error body", markers=markers, save_ext="json")
            f["requests"].append(r5b)
            r5 = r5b
    elif recipient_id and p.host_counts.get(host, 0) < MAX_PER_HOST:
        r6 = p.request(host, f"{API}/api/v2/recipient/{recipient_id}/",
                       note="recipient profile via resolver id", markers=markers, save_ext="json")
        f["requests"].append(r6)

    search_ok = any(
        r.get("status") == 200 and r.get("classification") == "server-rendered-json"
        for r in f["requests"] if "spending_by_award" in r.get("url", "")
    )
    recipient_ok = r2.get("status") == 200
    autocomplete_ok = r3.get("status") == 200
    if any(r.get("unreachable") for r in f["requests"]):
        f["verdict"] = "INCONCLUSIVE-network"
    elif search_ok and (recipient_ok or autocomplete_ok):
        f["verdict"] = "GO plain-fetch"
    elif search_ok:
        f["verdict"] = "GO plain-fetch (search only; recipient endpoints unavailable)"
    elif recipient_ok or autocomplete_ok:
        f["verdict"] = "GO plain-fetch (recipients only; award search unavailable)"
    else:
        f["verdict"] = "STUB fixture-only"
    return f


PROBES = {
    "trustpilot": probe_trustpilot,
    "softwareadvice": probe_softwareadvice,
    "getapp": probe_getapp,
    "usaspending": probe_usaspending,
}


def main() -> None:
    ap = argparse.ArgumentParser(description="P3 Task 14 paced new-source probe")
    ap.add_argument("--host", action="append", choices=sorted(PROBES),
                    help="probe only these hosts (repeatable); default: all")
    args = ap.parse_args()
    hosts = args.host or list(PROBES)

    p = Pacer()
    findings: dict[str, dict] = {}
    for host in hosts:
        print(f"=== {host} ===", flush=True)
        findings[host] = PROBES[host](p)
        print(f"  VERDICT: {findings[host]['verdict']}", flush=True)

    out_path = OUT_DIR / "p3_source_spike_2026_09_findings.json"
    merged: dict = {}
    if out_path.exists():
        try:
            merged = json.loads(out_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            merged = {}
    merged.update(findings)
    merged["_meta"] = {
        "date": "2026-09-04",
        "user_agent": UA,
        "constraints": {
            "max_requests_per_host": MAX_PER_HOST,
            "min_gap_between_requests_s": PACE_S,
            "timeout_s": TIMEOUT_S,
            "max_attempts_unreachable": MAX_ATTEMPTS_UNREACHABLE,
            "no_challenge_bypass": True,
        },
        "requests_per_host_this_process": p.host_counts,
    }
    out_path.write_text(json.dumps(merged, indent=2), encoding="utf-8")

    print("\n=== VERDICT JSON ===")
    print(json.dumps({
        "verdicts": {h: findings[h]["verdict"] for h in hosts},
        "requests_per_host_this_process": p.host_counts,
        "findings_file": str(out_path),
    }, indent=2))


if __name__ == "__main__":
    main()
