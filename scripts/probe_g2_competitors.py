"""T5 probe: G2 competitors/alternatives page -- GO/NO-GO for the T6
competitor-discovery pass (plan:
.zcode/plans/2026-09-05_205645-keyless-clay-exa-clones.md, Wave 2 Task T5).

Paced LIVE probe with a STRICT budget. DataDome escalates under repeated
probing, so the whole run is hard-capped:

  * AT MOST 3 total G2 fetches across all rungs (plain + browser + optional
    second target). The budget driver refuses to spend past the cap -- there
    is no code path that issues a 4th G2 request.
  * >= PACE_S (5.0) seconds between ANY two network requests, global across
    hosts (template: scripts/p2_source_spike.py).
  * KNOWN FLAKE RULE: one transient timeout retries that single request ONCE,
    consuming budget; a second failure is the recorded verdict.

Rungs:
  1. plain   -- curl_cffi impersonate="chrome" GET of the slack
                competitors/alternatives page (the production T6 curl-tier
                shape: src/core/curl_fetcher.py + capterra_resolve
                ._default_fetcher precedent). Body-validated, never
                status-validated: challenge markers (datadome / captcha /
                geo.captcha-delivery.com) vs real G2 markup (product-card /
                link-product-card / /products/<slug> hrefs).
  2. browser -- ONLY if the plain rung failed. One capture via the same
                machinery as scripts/probe_g2_reviews.py (patchright chromium,
                headed, config browser context, data/g2_cookies.json session
                cookies injected). Saves up to ~300 KB of rendered HTML to
                data/probe/g2_competitors_slack.html.
  3. parse-surface -- OFFLINE (zero network): on whichever rung returned real
                markup, determine HOW competitor data is exposed:
                (i)  server-rendered anchors matching /products/<slug>
                     (counted; first 10 slugs + names from link text);
                (ii) embedded JSON (__NEXT_DATA__ / self.__next_f / elv-*
                     fragments like the reviews page uses);
                (iii) nothing parseable.
                The exact selector/fragment path recorded here is what T6
                builds its parser on.
  4. optional second target -- hubspot-marketing-hub competitors/alternatives,
                same tier as whichever slack capture succeeded, ONLY if slack
                succeeded and request budget remains (confirms the URL shape
                generalizes).

Usage: ./.venv/Scripts/python.exe scripts/probe_g2_competitors.py
Writes: data/probe/g2_competitors_slack_plain.html (plain-tier body evidence),
        data/probe/g2_competitors_slack.html (browser capture),
        data/probe/g2_competitors_hubspot.html (optional second target) +
        data/probe/g2_competitors_findings.json (consumed by the verdict MD).
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.core.curl_fetcher import CurlCffiFetcher  # noqa: E402

OUT_DIR = ROOT / "data" / "probe"
OUT_DIR.mkdir(parents=True, exist_ok=True)

PACE_S = 5.0
MAX_G2_FETCHES = 3
HTML_SAVE_CAP = 300_000
GOTO_TIMEOUT_MS = 45_000
POLL_SECONDS = 30

SLACK_URL = "https://www.g2.com/products/slack/competitors/alternatives"
SLACK_LEGACY_URL = "https://www.g2.com/products/slack/alternatives"
HUBSPOT_URL = "https://www.g2.com/products/hubspot-marketing-hub/competitors/alternatives"

# Same UA the production curl tier ships (capterra_resolve._default_fetcher /
# config browser UA): matching UA to the chrome TLS fingerprint is the point
# of the impersonate tier.
CURL_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")

# Longest-first so substring hits never shadow a more specific marker.
CHALLENGE_MARKERS = (
    "geo.captcha-delivery.com",
    "captcha-delivery.com",
    "datadome",
    "dd-challenge",
    "px-captcha",
    "captcha",
)

PRODUCT_HREF_RE = re.compile(
    r"^(?:https?://(?:www\.)?g2\.com)?/products/([a-z0-9][a-z0-9-]*)(/[^?#]*)?(?:[?#].*)?$",
    re.I,
)


class Pacer:
    """Enforces >= min_gap seconds between ANY two network requests, global."""

    def __init__(self, min_gap_s: float):
        self.min_gap = min_gap_s
        self._last = 0.0  # monotonic; 0 => first request never waits

    def wait(self) -> None:
        delta = time.monotonic() - self._last
        if delta < self.min_gap:
            time.sleep(self.min_gap - delta)
        self._last = time.monotonic()


class Budget:
    """Hard cap on total G2 fetches for the whole run."""

    def __init__(self, max_g2: int):
        self.max_g2 = max_g2
        self.used = 0
        self.spent_labels: list[str] = []

    def remaining(self) -> int:
        return self.max_g2 - self.used

    def spend(self, label: str) -> bool:
        if self.remaining() <= 0:
            return False
        self.used += 1
        self.spent_labels.append(label)
        return True


def classify_body(text: str) -> dict:
    """Body classification. PURE. Challenge markers beat real-markup markers."""
    low = text.lower()
    hits = [m for m in CHALLENGE_MARKERS if m in low]
    real = {m: text.count(m) for m in ("product-card", "link-product-card")}
    prod_hrefs = len(re.findall(
        r"href=\"(?:https?://(?:www\.)?g2\.com)?/products/[a-z0-9-]+", low))
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
    title = " ".join(m.group(1).split())[:120] if m else None
    if hits:
        kind = "challenge"
    elif real["link-product-card"] or real["product-card"] or prod_hrefs >= 3:
        kind = "real-markup"
    else:
        kind = "other"
    return {"kind": kind, "challenge_markers": hits, "real_markers": real,
            "product_hrefs": prod_hrefs, "title": title}


def parse_surface(html: str, subject_slug: str) -> dict:
    """Offline parse-surface analysis. PURE (no network).

    Determines how competitor data is exposed on the page:
    (i) server-rendered /products/<slug> anchors, (ii) embedded JSON
    (__NEXT_DATA__ / __next_f / elv-* fragments), (iii) nothing parseable.
    """
    out: dict = {}

    # (ii) embedded JSON candidates
    m = re.search(r"<script id=\"__NEXT_DATA__\"[^>]*>(.*?)</script>", html, re.S)
    out["next_data_present"] = bool(m)
    out["next_data_buildid"] = None
    if m:
        try:
            data = json.loads(m.group(1))
            out["next_data_buildid"] = data.get("buildId")
            out["next_data_json"] = "parses"
        except Exception as e:  # noqa: BLE001
            out["next_data_json"] = f"json error: {e!r}"[:120]
    out["next_fragments_self_next_f"] = html.count("self.__next_f")
    elv = sorted({t for t in re.findall(r"elv-[a-z0-9-]+", html)})
    out["elv_classes"] = {"distinct": len(elv), "sample": elv[:12]}

    # (i) server-rendered anchors
    soup = BeautifulSoup(html, "html.parser")
    seen: list[dict] = []
    slugs_seen: set[str] = set()
    total = bare = 0
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        m2 = PRODUCT_HREF_RE.match(href)
        if not m2:
            continue
        total += 1
        slug = m2.group(1).lower()
        path = (m2.group(2) or "").strip().lower()
        is_bare = path in ("", "/")
        if is_bare:
            bare += 1
        if slug == subject_slug or slug in slugs_seen:
            continue
        slugs_seen.add(slug)
        seen.append({
            "slug": slug,
            "name": " ".join(a.get_text(" ", strip=True).split())[:80],
            "bare_link": is_bare,
            "href": href[:100],
        })
    out["anchors"] = {
        "total_products_hrefs": total,
        "bare_product_links": bare,
        "distinct_slugs_excl_subject": len(seen),
        "first10": seen[:10],
    }

    # Card container evidence for T6's selector
    card = soup.find(class_=re.compile(r"link-product-card|product-card"))
    if card is not None:
        out["card_container"] = {
            "tag": card.name,
            "class": " ".join(card.get("class", []))[:120],
            "snippet": str(card)[:600],
        }

    # Which exposure wins
    if out["anchors"]["distinct_slugs_excl_subject"] >= 3:
        out["exposure"] = "server-rendered-html"
        out["exact_path"] = ("a[href=/products/<slug>] inside card containers "
                             "class~=link-product-card (see card_container)")
    elif out["next_data_present"] and out.get("next_data_json") == "parses":
        out["exposure"] = "embedded-json"
        out["exact_path"] = "script#__NEXT_DATA__ (buildId=" + str(out["next_data_buildid"]) + ")"
    elif out["next_fragments_self_next_f"] > 0:
        out["exposure"] = "embedded-json"
        out["exact_path"] = "self.__next_f push fragments (streamed RSC payload)"
    else:
        out["exposure"] = "nothing-parseable"
        out["exact_path"] = None
    return out


def _save_html(name: str, text: str) -> str:
    """Save up to HTML_SAVE_CAP bytes; return repo-relative artifact path."""
    p = OUT_DIR / name
    p.write_text(text[:HTML_SAVE_CAP], encoding="utf-8")
    return f"data/probe/{name}"


def _is_transient(e: Exception) -> bool:
    """KNOWN FLAKE RULE scope: timeouts (and only timeouts) retry once."""
    s = f"{type(e).__name__}: {e}".lower()
    return "timeout" in s or "timed out" in s


def plain_rung(label: str, url: str, budget: Budget, pacer: Pacer,
               req_log: list) -> tuple[dict, str | None]:
    """Rung 1: curl_cffi impersonate='chrome'. Returns (record, html|None)."""
    rec: dict = {"rung": "plain-curl", "label": label, "url": url,
                 "tier": "curl_cffi impersonate=chrome"}
    if budget.remaining() <= 0:
        rec["skipped"] = "g2 budget exhausted"
        return rec, None
    fetcher = CurlCffiFetcher(user_agent=CURL_UA, impersonate="chrome")
    resp = err = None
    for attempt in (1, 2):
        if budget.remaining() <= 0:
            rec["skipped"] = "g2 budget exhausted"
            return rec, None
        pacer.wait()
        budget.spend(f"plain:{label}:attempt{attempt}")
        req_log.append({"label": f"{label} plain attempt {attempt}",
                        "url": url, "tier": "curl_cffi-chrome",
                        "g2_fetch": budget.used})
        try:
            resp = fetcher.get(url, headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
            err = None
            break
        except Exception as e:  # noqa: BLE001
            err = e
            if not _is_transient(e):
                break  # non-transient: verdict, no retry
            # transient -> one retry (attempt 2), still within budget
    if err is not None:
        rec["error"] = repr(err)[:300]
        rec["verdict"] = "ERROR"
        print(f"  [plain {label}] ERROR {err!r}", flush=True)
        return rec, None
    text = resp.body.decode("utf-8", "replace")
    cls = classify_body(text)
    rec.update({
        "status": resp.status,
        "final_url": str(resp.url),
        "bytes": len(text),
        "classification": cls,
        "artifact": _save_html(f"g2_competitors_{label}_plain.html", text),
        "verdict": {"challenge": "CHALLENGE-BLOCKED",
                    "real-markup": "REAL-MARKUP",
                    "other": "OTHER-STATUS-OR-BODY"}[cls["kind"]],
    })
    print(f"  [plain {label}] status={resp.status} bytes={len(text)} "
          f"-> {rec['verdict']} markers={cls['real_markers']} "
          f"challenge={cls['challenge_markers'][:2]}", flush=True)
    return rec, (text if cls["kind"] == "real-markup" else None)


def browser_rung(label: str, url: str, budget: Budget, pacer: Pacer,
                 req_log: list) -> tuple[dict, str | None]:
    """Rung 2: one patchright capture (machinery of scripts/probe_g2_reviews.py).

    Exactly ONE navigation = ONE G2 fetch; a transient navigation timeout
    retries ONCE within budget (KNOWN FLAKE RULE). A DataDome interstitial
    that never clears is the recorded verdict -- never reloaded.
    """
    rec: dict = {"rung": "browser-patchright", "label": label, "url": url,
                 "tier": "patchright chromium headed + session cookies"}
    from src.core.config import Config
    cfg = Config.load()
    cookies_path = ROOT / "data" / "g2_cookies.json"
    ctx_cookies: list[dict] = []
    if cookies_path.exists():
        try:
            raw = json.loads(cookies_path.read_text(encoding="utf-8"))
            ctx_cookies = [{"name": c["name"], "value": c["value"],
                            "domain": c["domain"], "path": "/"} for c in raw]
        except Exception as e:  # noqa: BLE001
            rec["cookie_inject_err"] = repr(e)[:200]
    rec["cookies_injected"] = [c["name"] for c in ctx_cookies]

    from patchright.sync_api import sync_playwright
    try:
        pw_ctx = sync_playwright().start()
    except Exception as e:  # noqa: BLE001
        rec["verdict"] = "NO-GO-for-now (playwright start failed)"
        rec["launcher_error"] = repr(e)[:400]
        print(f"  [browser {label}] LAUNCHER ERROR {e!r}", flush=True)
        return rec, None
    browser = None
    try:
        try:
            browser = pw_ctx.chromium.launch(
                headless=False,
                args=["--disable-blink-features=AutomationControlled",
                      "--no-sandbox"],
            )
        except Exception as e:  # noqa: BLE001
            rec["verdict"] = "NO-GO-for-now (browser launcher error)"
            rec["launcher_error"] = repr(e)[:400]
            print(f"  [browser {label}] LAUNCHER ERROR {e!r}", flush=True)
            return rec, None
        context = browser.new_context(
            viewport={"width": cfg.browser.viewport["width"],
                      "height": cfg.browser.viewport["height"]},
            locale=cfg.browser.locale,
            timezone_id=cfg.browser.timezone,
            user_agent=cfg.browser.user_agent,
        )
        if ctx_cookies:
            context.add_cookies(ctx_cookies)
        page = context.new_page()

        netlog: list[dict] = []

        def on_response(resp):
            try:
                if "g2.com" in resp.url and re.search(
                        r"competitors|alternatives|card|fragment|div_|page-data",
                        resp.url):
                    netlog.append({"url": resp.url[:160], "status": resp.status,
                                   "ctype": resp.headers.get(
                                       "content-type", "").split(";")[0]})
            except Exception:  # noqa: BLE001
                pass

        page.on("response", on_response)

        # --- the one G2 fetch of this rung (+1 allowed flake retry) ---
        nav_err = None
        for attempt in (1, 2):
            if budget.remaining() <= 0:
                rec["skipped"] = "g2 budget exhausted"
                return rec, None
            pacer.wait()
            budget.spend(f"browser:{label}:goto attempt {attempt}")
            req_log.append({"label": f"{label} browser goto attempt {attempt}",
                            "url": url, "tier": "patchright-headed",
                            "g2_fetch": budget.used})
            try:
                page.goto(url, wait_until="domcontentloaded",
                          timeout=GOTO_TIMEOUT_MS)
                nav_err = None
                break
            except Exception as e:  # noqa: BLE001
                nav_err = e
                if not _is_transient(e):
                    break
        if nav_err is not None:
            rec["error"] = repr(nav_err)[:300]
            rec["verdict"] = "NAVIGATION-ERROR"
            print(f"  [browser {label}] NAV ERROR {nav_err!r}", flush=True)
            return rec, None

        # --- poll for real markup; DataDome may auto-clear or persist ---
        html = ""
        marker = None
        for i in range(POLL_SECONDS):
            time.sleep(1)
            try:
                page.mouse.wheel(0, 1200)
            except Exception:  # noqa: BLE001
                pass
            html = page.content()
            if (html.count("link-product-card") > 0
                    or html.count("product-card") > 0):
                marker = "product-card"
                break
        if marker is None:
            html = page.content()

        cls = classify_body(html)
        rec.update({
            "url_after": page.url,
            "title": page.title()[:120],
            "bytes": len(html),
            "render_wait_s": POLL_SECONDS if marker is None else i + 1,
            "render_marker": marker,
            "classification": cls,
            "artifact": _save_html(f"g2_competitors_{label}.html", html),
            "verdict": {"challenge": "CHALLENGE-BLOCKED",
                        "real-markup": "REAL-MARKUP",
                        "other": "OTHER-BODY"}[cls["kind"]],
            "xhr_netlog": netlog[:20],
        })
        print(f"  [browser {label}] url_after={page.url[:80]} "
              f"bytes={len(html)} -> {rec['verdict']} "
              f"markers={cls['real_markers']}", flush=True)
        return rec, (html if cls["kind"] == "real-markup" else None)
    finally:
        try:
            if browser is not None:
                browser.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            pw_ctx.stop()
        except Exception:  # noqa: BLE001
            pass


def main() -> None:
    print("=== T5 probe: G2 competitors/alternatives (budget: "
          f"{MAX_G2_FETCHES} G2 fetches, pace {PACE_S}s) ===", flush=True)
    pacer = Pacer(PACE_S)
    budget = Budget(MAX_G2_FETCHES)
    req_log: list[dict] = []
    rungs: list[dict] = []

    # Rung 1: plain curl tier (slack)
    print("--- rung 1: plain curl tier ---", flush=True)
    plain_rec, slack_html = plain_rung("slack", SLACK_URL, budget, pacer, req_log)
    rungs.append(plain_rec)
    best_html = slack_html
    best_source = "plain" if slack_html is not None else None

    # Reality-wins fallback: /competitors/alternatives 404 -> legacy path ONCE
    if (plain_rec.get("status") == 404 and best_html is None
            and budget.remaining() > 0):
        print("--- rung 1b: legacy /alternatives path (plain, once) ---",
              flush=True)
        legacy_rec, legacy_html = plain_rung("slack_legacy", SLACK_LEGACY_URL,
                                             budget, pacer, req_log)
        rungs.append(legacy_rec)
        if legacy_html is not None:
            best_html, best_source = legacy_html, "plain"

    # Rung 2: browser tier, ONLY if the plain rung failed
    if best_html is None and budget.remaining() > 0:
        print("--- rung 2: browser patchright tier ---", flush=True)
        brec, bhtml = browser_rung("slack", SLACK_URL, budget, pacer, req_log)
        rungs.append(brec)
        if bhtml is not None:
            best_html, best_source = bhtml, "browser"

    # Rung 3: parse-surface (OFFLINE -- zero network)
    parse = None
    if best_html is not None:
        print("--- rung 3: parse-surface (offline) ---", flush=True)
        parse = parse_surface(best_html, "slack")
        parse["source_rung"] = best_source
        print(f"  exposure={parse['exposure']} "
              f"distinct_slugs={parse['anchors']['distinct_slugs_excl_subject']} "
              f"bare_links={parse['anchors']['bare_product_links']}", flush=True)
        for entry in parse["anchors"]["first10"][:5]:
            print(f"    {entry['slug']}: {entry['name'][:60]}", flush=True)

    # Rung 4: optional second target (only on a slack success + budget left)
    hubspot_html = None
    if best_html is not None and budget.remaining() > 0:
        print("--- rung 4: second target hubspot-marketing-hub ---", flush=True)
        if best_source == "plain":
            hrec, hubspot_html = plain_rung("hubspot", HUBSPOT_URL, budget,
                                            pacer, req_log)
        else:
            hrec, hubspot_html = browser_rung("hubspot", HUBSPOT_URL, budget,
                                              pacer, req_log)
        rungs.append(hrec)
        if hubspot_html is not None:
            hparse = parse_surface(hubspot_html, "hubspot-marketing-hub")
            hrec["parse_surface"] = {
                "exposure": hparse["exposure"],
                "distinct_slugs_excl_subject":
                    hparse["anchors"]["distinct_slugs_excl_subject"],
                "bare_product_links": hparse["anchors"]["bare_product_links"],
                "first5_slugs": [e["slug"] for e in
                                 hparse["anchors"]["first10"][:5]],
            }

    # Overall verdict
    if best_source == "plain":
        overall = "GO curl-tier"
    elif best_source == "browser":
        overall = "GO browser-tier"
    else:
        overall = "NO-GO"

    findings = {
        "probe": "g2_competitors",
        "task": "T5",
        "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "target_url": SLACK_URL,
        "optional_target_url": HUBSPOT_URL,
        "budget": {
            "max_g2_fetches": MAX_G2_FETCHES,
            "g2_fetches_used": budget.used,
            "spent_on": budget.spent_labels,
            "pace_s_between_any_two_requests": PACE_S,
            "request_log": req_log,
        },
        "overall_verdict": overall,
        "parse_surface": parse,
        "rungs": rungs,
    }
    out_path = OUT_DIR / "g2_competitors_findings.json"
    out_path.write_text(json.dumps(findings, indent=2, default=str),
                        encoding="utf-8")
    print(f"\noverall: {overall}; parse exposure: "
          f"{(parse or {}).get('exposure')}", flush=True)
    print(f"budget: {budget.used}/{MAX_G2_FETCHES} G2 fetches "
          f"({', '.join(budget.spent_labels)})", flush=True)
    print(f"findings -> {out_path}", flush=True)


if __name__ == "__main__":
    main()
