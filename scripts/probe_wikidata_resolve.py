"""T1 probe spike: keyless identity discovery (Wikidata -> Wikipedia -> GKG -> DDG).

One paced reconnaissance pass to decide each identity-waterfall rung BEFORE any
build (plan: .zcode/plans/2026-09-05_205645-keyless-clay-exa-clones.md, Task T1).

Rungs:
  0. robots.txt allow/deny for the exact paths the waterfall needs
     (wikidata/wikipedia /w/api.php, bing /news/search).
  1. Wikidata wbsearchentities -> wbgetclaims P856 (official website).
  2. Wikipedia list=search -> prop=extlinks (stripe.com present?).
  3. Google Enterprise Knowledge Graph (no credentials exist on this machine ->
     expect 401/403; documents the unauthenticated error shape).
     Verdict = PENDING-CREDENTIALS.
  4. Legacy Knowledge Graph Search API, keyless (documents the 403
     PERMISSION_DENIED key gate; with a key it returns itemListElement[].result.url).
  5. DDG html SERP ladder for "stripe inc official website":
     (a) plain httpx tier -> expect 202/challenge body;
     (b) curl_cffi impersonate="chrome" tier -> body-validated (challenge markers
         vs result__a anchors), NEVER status-validated (DDG ships challenge pages
         with 200/202);
     (c) browser/patchright tier -> untested, reserved.

Anti-bot discipline (template: scripts/p2_source_spike.py):
  * ONE request per probe URL; a single flake retry per request is allowed
    (KNOWN FLAKE RULE: one timeout, e.g. Windows os error 10060, then give up).
  * PACE_S = 5.0 s between ANY two consecutive requests, global across the run.
  * Honest UA (repo convention: src/core/http.py -> config.resolved_user_agent(),
    "SignalsResearchBot/0.1 (+contact: <SIGNALS_CONTACT_EMAIL>)") on every httpx
    request. The curl_cffi tier keeps the chrome-impersonation UA that ships with
    impersonate="chrome" -- matching UA to TLS fingerprint is the point of that
    tier and mirrors the production T3c stack (src/core/curl_fetcher.py).
  * <=3 requests per host per rung (disclosed in the findings JSON).

Usage: python scripts/probe_wikidata_resolve.py
Writes: data/probe/keyless_identity_*.{json,html,txt} raw dumps +
        data/probe/keyless_identity_findings.json (consumed by the verdict MD).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path
from urllib.robotparser import RobotFileParser

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

OUT_DIR = Path("data/probe")
OUT_DIR.mkdir(parents=True, exist_ok=True)

PACE_S = 5.0
TIMEOUT = 30.0
PRODUCT_TOKEN = "SignalsResearchBot"

UA_TEMPLATE = "SignalsResearchBot/0.1 (+contact: {email})"  # config/default.yaml http.user_agent


def _contact_email() -> str:
    """SIGNALS_CONTACT_EMAIL from env, else .env (config._apply_env_overrides precedent)."""
    email = os.environ.get("SIGNALS_CONTACT_EMAIL")
    if email:
        return email
    env_file = Path(__file__).resolve().parents[1] / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("SIGNALS_CONTACT_EMAIL") and "=" in line:
                return line.partition("=")[2].strip()
    raise SystemExit("SIGNALS_CONTACT_EMAIL not set (env or .env) — refusing to send REPLACE_ME UA")


UA = UA_TEMPLATE.format(email=_contact_email())
JSON_HEADERS = {"Accept": "application/json"}

# --- DDG body-validation markers (plan T1: validate BODY, never status) -------
DDG_CHALLENGE_MARKERS = (
    "unfortunately, bots use duckduckgo",
    "anomaly-detected",
    "anomaly",
    "captcha",
)
DDG_SUCCESS_MARKER = 'class="result__a"'
RESULT_A_HREF_RE = re.compile(r"<a\s[^>]*>", re.I)

# --- Wikidata entity pick heuristic ------------------------------------------
STRIPE_HINTS = (
    "payment", "financial", "fintech", "software", "technology",
    "saas", "internet", "online", "commerce", "services",
)


def looks_like_stripe(entity: dict) -> bool:
    """Label/description heuristic for the payments company. PURE."""
    text = f'{entity.get("label", "")} {entity.get("description", "")}'.lower()
    if "stripe" not in text:
        return False
    return any(h in text for h in STRIPE_HINTS)


# --- robots.txt evaluation (pure, no network) --------------------------------
def robots_groups(robots_text: str) -> list[dict]:
    """Split robots.txt into {agents: [...], rules: [(kind, value), ...]} groups."""
    groups: list[dict] = []
    cur: dict | None = None
    last_was_agent = False
    for raw in robots_text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip().lower()
        val = val.strip()
        if key == "user-agent":
            if cur is None or not last_was_agent:
                cur = {"agents": [], "rules": []}
                groups.append(cur)
            cur["agents"].append(val)
            last_was_agent = True
        elif key in ("allow", "disallow"):
            if cur is None:
                cur = {"agents": ["*"], "rules": []}
                groups.append(cur)
            cur["rules"].append((key, val))
            last_was_agent = False
        else:
            # crawl-delay and friends: a rule line, so a new User-agent line
            # after it starts a NEW group (do not merge groups across it).
            last_was_agent = False
    return groups


def _rule_matches(rule: str, path: str) -> bool:
    """robots rule -> path: plain prefix, '*' wildcard, '$' anchor. PURE."""
    if not rule:
        return False
    anchored = rule.endswith("$")
    body = rule[:-1] if anchored else rule
    rx = re.escape(body).replace(r"\*", ".*")
    if anchored:
        return re.match(rx + r"$", path) is not None
    return re.match(rx, path) is not None  # re.match == prefix semantics


def robots_eval(robots_text: str, path: str, product_token: str) -> dict:
    """Applicable group + quoted matching Allow/Disallow lines for one path. PURE."""
    groups = robots_groups(robots_text)
    tok = product_token.lower()
    chosen: dict | None = None
    for g in groups:  # last matching group wins (RobotFileParser semantics)
        if any(a.lower() == tok for a in g["agents"]):
            chosen = g
    if chosen is None:
        for g in groups:
            if "*" in g["agents"]:
                chosen = g  # last '*' group wins
    if chosen is None:
        return {"applicable_agents": [], "matched_lines": [], "note": "no groups parsed"}
    matched = [f"{k.capitalize()}: {v}" for k, v in chosen["rules"] if _rule_matches(v, path)]
    note = ""
    if not matched:
        note = "no rule in the applicable group matches the path -> allowed by default"
    return {"applicable_agents": chosen["agents"], "matched_lines": matched, "note": note}


# --- global pacing + flake-retry fetch helpers --------------------------------
_REQ_LOG: list[dict] = []
_last_start: float | None = None


def _pace() -> None:
    """Sleep so consecutive request STARTS are >= PACE_S apart, anywhere in the run."""
    global _last_start
    now = time.monotonic()
    if _last_start is not None:
        wait = PACE_S - (now - _last_start)
        if wait > 0:
            print(f"    pace: sleeping {wait:.1f}s", flush=True)
            time.sleep(wait)
    _last_start = time.monotonic()


def paced_get(client: httpx.Client, url: str, *, rung: int, label: str,
              headers: dict | None = None) -> httpx.Response:
    """One paced GET with a single flake retry (KNOWN FLAKE RULE)."""
    host = urllib.parse.urlsplit(url).hostname or ""
    last_exc: Exception | None = None
    for attempt in (1, 2):
        _pace()
        t0 = time.monotonic()
        try:
            r = client.get(url, headers=headers)
            ms = int((time.monotonic() - t0) * 1000)
            _REQ_LOG.append({"rung": rung, "host": host, "url": url,
                             "attempt": attempt, "status": r.status_code, "ms": ms})
            print(f"    [{label}] {r.status_code} ({len(r.content)}B, {ms}ms)", flush=True)
            return r
        except httpx.TransportError as exc:  # timeouts/connect flake (10060 etc.)
            last_exc = exc
            _REQ_LOG.append({"rung": rung, "host": host, "url": url,
                             "attempt": attempt, "error": repr(exc)})
            print(f"    [{label}] transport error attempt {attempt}: {exc!r}", flush=True)
    assert last_exc is not None
    raise last_exc


def paced_curl_get(url: str, *, rung: int, label: str):
    """One paced curl_cffi GET (chrome impersonation) with a single flake retry."""
    from curl_cffi import requests as curl_requests

    host = urllib.parse.urlsplit(url).hostname or ""
    last_exc: Exception | None = None
    for attempt in (1, 2):
        _pace()
        t0 = time.monotonic()
        try:
            r = curl_requests.get(url, impersonate="chrome", timeout=30)
            ms = int((time.monotonic() - t0) * 1000)
            _REQ_LOG.append({"rung": rung, "host": host, "url": url,
                             "attempt": attempt, "status": r.status_code, "ms": ms})
            print(f"    [{label}] {r.status_code} ({len(r.content)}B, {ms}ms, curl tier)", flush=True)
            return r
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            _REQ_LOG.append({"rung": rung, "host": host, "url": url,
                             "attempt": attempt, "error": repr(exc)})
            print(f"    [{label}] curl error attempt {attempt}: {exc!r}", flush=True)
    assert last_exc is not None
    raise last_exc


# --- rungs --------------------------------------------------------------------
ROBOTS_TARGETS = [
    ("https://www.wikidata.org/robots.txt", "/w/api.php"),
    ("https://en.wikipedia.org/robots.txt", "/w/api.php"),
    ("https://www.bing.com/robots.txt", "/news/search"),
]


def rung_robots(client: httpx.Client) -> dict:
    rec: dict = {"rung": 0, "name": "robots", "checks": [], "artifacts": []}
    for robots_url, path in ROBOTS_TARGETS:
        host = urllib.parse.urlsplit(robots_url).hostname or ""
        entry: dict = {"host": host, "robots_url": robots_url, "path": path}
        try:
            r = paced_get(client, robots_url, rung=0, label=f"robots:{host}")
            fname = f"keyless_identity_robots_{host.replace('.', '_')}.txt"
            (OUT_DIR / fname).write_text(r.text, encoding="utf-8")
            rec["artifacts"].append(f"data/probe/{fname}")
            entry["status"] = r.status_code
            if r.status_code != 200 or not r.text.strip():
                entry["allowed"] = None
                entry["note"] = f"robots.txt HTTP {r.status_code} -> no rules retrievable (treat as allowed)"
            else:
                rp = RobotFileParser()
                rp.parse(r.text.splitlines())
                test_url = f"https://{host}{path}"
                entry["allowed"] = rp.can_fetch(UA, test_url)
                ev = robots_eval(r.text, path, PRODUCT_TOKEN)
                entry["applicable_agents"] = ev["applicable_agents"]
                entry["matched_lines"] = ev["matched_lines"]
                if ev["note"]:
                    entry["note"] = ev["note"]
        except Exception as exc:  # noqa: BLE001
            entry["error"] = repr(exc)
            entry["allowed"] = None
        rec["checks"].append(entry)
    ok = [c for c in rec["checks"] if c.get("allowed") is True]
    blocked = [c for c in rec["checks"] if c.get("allowed") is False]
    if blocked:
        rec["verdict"] = f"PARTIAL: {len(blocked)} path(s) robots-DISALLOWED for our UA (scoped robots_allow override needed)"
    elif len(ok) == len(ROBOTS_TARGETS):
        rec["verdict"] = "GO: all target paths robots-allowed for our UA"
    else:
        rec["verdict"] = "UNKNOWN: robots checks errored"
    return rec


def rung_wikidata(client: httpx.Client) -> dict:
    rec: dict = {"rung": 1, "name": "wikidata",
                 "urls": [], "artifacts": [], "evidence": {}}
    search_url = ("https://www.wikidata.org/w/api.php?action=wbsearchentities"
                  "&search=Stripe&language=en&type=item&format=json&limit=5")
    rec["urls"].append(search_url)
    r = paced_get(client, search_url, rung=1, label="wbsearchentities", headers=JSON_HEADERS)
    try:
        data = r.json()
    except ValueError:
        rec["verdict"] = f"NO-GO: wbsearchentities returned HTTP {r.status_code} non-JSON body"
        rec["evidence"]["body_head"] = r.text[:300]
        return rec
    (OUT_DIR / "keyless_identity_wikidata_search.json").write_text(
        json.dumps(data, indent=2), encoding="utf-8")
    rec["artifacts"].append("data/probe/keyless_identity_wikidata_search.json")

    results = data.get("search", [])
    rec["evidence"]["top5"] = [
        {"id": e.get("id"), "label": e.get("label"), "description": e.get("description", "")}
        for e in results
    ]
    picked = next((e for e in results if looks_like_stripe(e)), None)
    rec["evidence"]["pick_method"] = "first top-5 entity whose label/description matches payments hints"
    if picked is None and results:
        picked = results[0]
        rec["evidence"]["pick_method"] = "FALLBACK: no entity matched payments hints; took results[0]"
    if picked is None:
        rec["verdict"] = "NO-GO: empty search[] payload"
        return rec
    qid = picked.get("id", "")
    rec["evidence"]["picked"] = {"id": qid, "label": picked.get("label"),
                                 "description": picked.get("description", "")}
    rec["evidence"]["collisions_in_top5"] = [
        {"id": e.get("id"), "label": e.get("label"), "description": e.get("description", "")}
        for e in results if e.get("id") != qid
    ]

    claims_url = (f"https://www.wikidata.org/w/api.php?action=wbgetclaims&entity={qid}"
                  f"&property=P856&format=json")
    rec["urls"].append(claims_url)
    r2 = paced_get(client, claims_url, rung=1, label="wbgetclaims P856", headers=JSON_HEADERS)
    try:
        data2 = r2.json()
    except ValueError:
        rec["verdict"] = f"NO-GO: wbgetclaims returned HTTP {r2.status_code} non-JSON body"
        rec["evidence"]["body_head"] = r2.text[:300]
        return rec
    (OUT_DIR / "keyless_identity_wikidata_claims.json").write_text(
        json.dumps(data2, indent=2), encoding="utf-8")
    rec["artifacts"].append("data/probe/keyless_identity_wikidata_claims.json")

    claims = data2.get("claims", {}).get("P856", [])
    p856 = None
    if claims:
        try:
            p856 = claims[0]["mainsnak"]["datavalue"]["value"]
        except (KeyError, IndexError, TypeError):
            p856 = None
    rec["evidence"]["qid"] = qid
    rec["evidence"]["p856_value"] = p856
    rec["evidence"]["p856_claim_count"] = len(claims)
    host = urllib.parse.urlsplit(p856 or "").hostname or ""
    if host.lower().endswith("stripe.com"):
        rec["verdict"] = f"GO: {qid} P856 -> {p856}"
    else:
        rec["verdict"] = f"NO-GO: {qid} P856 -> {p856!r} (not stripe.com)"
    return rec


def rung_wikipedia(client: httpx.Client) -> dict:
    rec: dict = {"rung": 2, "name": "wikipedia",
                 "urls": [], "artifacts": [], "evidence": {}}
    search_url = ("https://en.wikipedia.org/w/api.php?action=query&list=search"
                  "&srsearch=Stripe%2C%20Inc.&format=json&srlimit=3")
    rec["urls"].append(search_url)
    r = paced_get(client, search_url, rung=2, label="list=search", headers=JSON_HEADERS)
    try:
        data = r.json()
    except ValueError:
        rec["verdict"] = f"NO-GO: list=search returned HTTP {r.status_code} non-JSON body"
        rec["evidence"]["body_head"] = r.text[:300]
        return rec
    (OUT_DIR / "keyless_identity_wikipedia_search.json").write_text(
        json.dumps(data, indent=2), encoding="utf-8")
    rec["artifacts"].append("data/probe/keyless_identity_wikipedia_search.json")

    hits = data.get("query", {}).get("search", [])
    rec["evidence"]["top_titles"] = [h.get("title") for h in hits]
    if not hits:
        rec["verdict"] = "NO-GO: empty search results"
        return rec
    title = hits[0].get("title", "")
    rec["evidence"]["top_title"] = title

    ext_url = ("https://en.wikipedia.org/w/api.php?action=query&titles="
               + urllib.parse.quote(title)
               + "&prop=extlinks&ellimit=50&format=json")
    rec["urls"].append(ext_url)
    r2 = paced_get(client, ext_url, rung=2, label="prop=extlinks", headers=JSON_HEADERS)
    try:
        data2 = r2.json()
    except ValueError:
        rec["verdict"] = f"NO-GO: extlinks returned HTTP {r2.status_code} non-JSON body"
        rec["evidence"]["body_head"] = r2.text[:300]
        return rec
    (OUT_DIR / "keyless_identity_wikipedia_extlinks.json").write_text(
        json.dumps(data2, indent=2), encoding="utf-8")
    rec["artifacts"].append("data/probe/keyless_identity_wikipedia_extlinks.json")

    pages = data2.get("query", {}).get("pages", {})
    links: list[str] = []
    for page in pages.values():
        for el in page.get("extlinks", []):
            url = next(iter(el.values()), "")
            if url:
                links.append(url)
    rec["evidence"]["extlink_count"] = len(links)
    rec["evidence"]["extlinks_sample"] = links[:8]
    stripe_links = [u for u in links
                    if (urllib.parse.urlsplit(u).hostname or "").lower().endswith("stripe.com")]
    rec["evidence"]["stripe_com_links"] = stripe_links
    if stripe_links:
        rec["verdict"] = f"GO: {len(stripe_links)} stripe.com extlink(s) on '{title}'"
    else:
        rec["verdict"] = f"NO-GO: no stripe.com extlink on '{title}' ({len(links)} extlinks)"
    return rec


def rung_ekg(client: httpx.Client) -> dict:
    rec: dict = {"rung": 3, "name": "ekg", "artifacts": [], "evidence": {}}
    url = ("https://enterpriseknowledgegraph.googleapis.com/v1/projects/test-project"
           "/locations/global/publicKnowledgeGraphEntities:Search"
           "?query=Stripe&types=Organization&languages=en&limit=5")
    rec["url"] = url
    try:
        r = paced_get(client, url, rung=3, label="ekg-search", headers=JSON_HEADERS)
    except Exception as exc:  # noqa: BLE001
        rec["verdict"] = "PENDING-CREDENTIALS (request failed before an HTTP status)"
        rec["evidence"]["error"] = repr(exc)
        return rec
    (OUT_DIR / "keyless_identity_ekg_error.json").write_text(r.text, encoding="utf-8")
    rec["artifacts"].append("data/probe/keyless_identity_ekg_error.json")
    rec["status"] = r.status_code
    try:
        err = r.json().get("error", {})
        rec["evidence"]["error_status"] = err.get("status")
        rec["evidence"]["error_message"] = (err.get("message") or "")[:300]
        rec["evidence"]["error_code"] = err.get("code")
    except ValueError:
        rec["evidence"]["body_head"] = r.text[:300]
    if r.status_code in (401, 403):
        rec["verdict"] = "PENDING-CREDENTIALS (unauthenticated shape documented; needs GOOGLE_APPLICATION_CREDENTIALS)"
    else:
        rec["verdict"] = f"UNEXPECTED: HTTP {r.status_code} (brief expected 401/403) — reality recorded"
    return rec


def rung_kgsearch(client: httpx.Client) -> dict:
    rec: dict = {"rung": 4, "name": "kgsearch", "artifacts": [], "evidence": {}}
    url = ("https://kgsearch.googleapis.com/v1/entities:search"
           "?query=Stripe&types=Organization&languages=en&limit=1")
    rec["url"] = url
    try:
        r = paced_get(client, url, rung=4, label="kgsearch", headers=JSON_HEADERS)
    except Exception as exc:  # noqa: BLE001
        rec["verdict"] = "ERROR: request failed"
        rec["evidence"]["error"] = repr(exc)
        return rec
    (OUT_DIR / "keyless_identity_kgsearch_error.json").write_text(r.text, encoding="utf-8")
    rec["artifacts"].append("data/probe/keyless_identity_kgsearch_error.json")
    rec["status"] = r.status_code
    try:
        err = r.json().get("error", {})
        rec["evidence"]["error_status"] = err.get("status")
        rec["evidence"]["error_message"] = (err.get("message") or "")[:300]
    except ValueError:
        rec["evidence"]["body_head"] = r.text[:300]
    if r.status_code == 403 and "PERMISSION_DENIED" in r.text:
        rec["verdict"] = "GATE-CONFIRMED: keyless call -> 403 PERMISSION_DENIED (with a key: itemListElement[].result.url)"
    else:
        rec["verdict"] = f"UNEXPECTED: HTTP {r.status_code}, PERMISSION_DENIED={'PERMISSION_DENIED' in r.text} — reality recorded"
    return rec


def ddg_classify(text: str) -> tuple[str, str]:
    """('challenge'|'results'|'unknown-shape', marker). Body-only, never status. PURE."""
    low = text[:20000].lower()
    for marker in DDG_CHALLENGE_MARKERS:
        if marker in low:
            return "challenge", marker
    if DDG_SUCCESS_MARKER in low:
        return "results", DDG_SUCCESS_MARKER
    return "unknown-shape", ""


def resolve_ddg_href(href: str) -> str:
    """Resolve /l/?uddg=<urlencoded> redirect wrappers to the target URL. PURE."""
    if "uddg=" not in href:
        return href
    if not href.startswith("http"):
        href = "https://duckduckgo.com" + (href if href.startswith("/") else "/" + href)
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query)
    if params.get("uddg"):
        return urllib.parse.unquote(params["uddg"][0])
    return href


def extract_ddg_results(html_text: str, n: int = 3) -> list[str]:
    """First n organic result URLs from result__a anchors. PURE."""
    out: list[str] = []
    for tag in RESULT_A_HREF_RE.findall(html_text):
        if "result__a" not in tag:
            continue
        m = re.search(r'href="([^"]+)"', tag, re.I)
        if not m:
            continue
        resolved = resolve_ddg_href(m.group(1))
        host = urllib.parse.urlsplit(resolved).hostname or ""
        if resolved.startswith("http") and "duckduckgo.com" not in host:
            out.append(resolved)
        if len(out) >= n:
            break
    return out


def rung_ddg(client: httpx.Client) -> dict:
    rec: dict = {"rung": 5, "name": "ddg-ladder", "artifacts": [], "evidence": {}}
    q = "stripe inc official website"
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote_plus(q)
    rec["url"] = url
    rec["query"] = q

    # (a) PLAIN tier: honest UA httpx — expect 202 or a challenge body
    plain_verdict = "ERROR"
    try:
        r = paced_get(client, url, rung=5, label="ddg-plain")
        (OUT_DIR / "keyless_identity_ddg_plain.html").write_text(
            r.text, encoding="utf-8", errors="replace")
        rec["artifacts"].append("data/probe/keyless_identity_ddg_plain.html")
        cls, marker = ddg_classify(r.text)
        rec["plain"] = {"status": r.status_code, "bytes": len(r.content),
                        "classification": cls, "challenge_marker": marker}
        if cls == "challenge":
            plain_verdict = f"CHALLENGED: {marker}"
        elif cls == "results":
            plain_verdict = "RESULTS: plain tier unexpectedly served real markup"
        else:
            plain_verdict = f"UNKNOWN-SHAPE: HTTP {r.status_code}, no marker hit"
    except Exception as exc:  # noqa: BLE001
        rec["plain"] = {"error": repr(exc)}
        plain_verdict = f"ERROR: {exc!r}"
    rec["plain_verdict"] = plain_verdict

    # (b) CURL_CFFI tier: chrome impersonation, validate the BODY not the status
    curl_verdict = "ERROR"
    try:
        r2 = paced_curl_get(url, rung=5, label="ddg-curl")
        body = r2.content
        (OUT_DIR / "keyless_identity_ddg_curl.html").write_bytes(body[:200_000])
        rec["artifacts"].append("data/probe/keyless_identity_ddg_curl.html")
        text = body.decode("utf-8", errors="replace")
        cls, marker = ddg_classify(text)
        n_results = text.lower().count("result__a")
        rec["curl"] = {"status": r2.status_code, "bytes": len(body),
                       "classification": cls, "challenge_marker": marker,
                       "result__a_anchor_count": n_results}
        if cls == "challenge":
            curl_verdict = f"CHALLENGED: {marker}"
        elif cls == "results":
            results = extract_ddg_results(text)
            rec["curl"]["top3_results"] = results
            first_host = urllib.parse.urlsplit(results[0]).hostname or "" if results else ""
            rec["curl"]["stripe_is_first"] = first_host.lower().endswith("stripe.com")
            curl_verdict = f"GO: real result markup ({n_results} result__a anchors); stripe.com #1 = {rec['curl']['stripe_is_first']}"
        else:
            curl_verdict = f"UNKNOWN-SHAPE: HTTP {r2.status_code}, no challenge/result marker in body"
    except Exception as exc:  # noqa: BLE001
        rec["curl"] = {"error": repr(exc)}
        curl_verdict = f"ERROR: {exc!r}"
    rec["curl_verdict"] = curl_verdict

    # (c) browser/patchright tier: NOT used in this probe
    rec["browser"] = "untested, reserved"

    if curl_verdict.startswith("GO"):
        rec["verdict"] = "GO for curl tier" if plain_verdict.startswith(("CHALLENGED", "UNKNOWN")) \
            else f"GO curl tier; plain tier also served results ({plain_verdict})"
    else:
        rec["verdict"] = f"NO-GO curl tier ({curl_verdict})"
    return rec


RUNGS = [
    ("robots", rung_robots),
    ("wikidata", rung_wikidata),
    ("wikipedia", rung_wikipedia),
    ("ekg", rung_ekg),
    ("kgsearch", rung_kgsearch),
    ("ddg-ladder", rung_ddg),
]


def main() -> None:
    started = time.monotonic()
    print(f"UA: {UA}", flush=True)
    client = httpx.Client(headers={"User-Agent": UA}, timeout=TIMEOUT, follow_redirects=True)
    rungs: list[dict] = []
    try:
        for name, fn in RUNGS:
            print(f"=== rung: {name} ===", flush=True)
            try:
                rec = fn(client)
            except Exception as exc:  # noqa: BLE001 — one rung must never abort the rest
                rec = {"name": name, "verdict": f"ERROR: {exc!r}"}
            print(f"  verdict: {rec.get('verdict')}", flush=True)
            rungs.append(rec)
    finally:
        client.close()

    # per-host-per-rung budget disclosure
    budget: dict[str, int] = {}
    for e in _REQ_LOG:
        key = f"rung{e['rung']}:{e['host']}"
        budget[key] = budget.get(key, 0) + 1
    findings = {
        "probe": "keyless_identity",
        "date": time.strftime("%Y-%m-%d"),
        "script": "scripts/probe_wikidata_resolve.py",
        "ua": UA,
        "pace_s": PACE_S,
        "rungs": rungs,
        "request_budget": {
            "per_host_per_rung_limit": 3,
            "observed": budget,
            "total_requests": len(_REQ_LOG),
            "request_log": _REQ_LOG,
        },
        "runtime_s": round(time.monotonic() - started, 1),
    }
    out_path = OUT_DIR / "keyless_identity_findings.json"
    out_path.write_text(json.dumps(findings, indent=2), encoding="utf-8")
    print(f"\nfindings -> {out_path}", flush=True)
    print(f"runtime: {findings['runtime_s']}s, requests: {len(_REQ_LOG)}", flush=True)
    for rung in rungs:
        print(f"  rung {rung.get('name')}: {rung.get('verdict')}", flush=True)


if __name__ == "__main__":
    main()
