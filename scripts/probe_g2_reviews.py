"""Probe G2 reviews data source (Task 1 spike) -- v3.

DataDome-intermittent. Navigates directly to the LIVE G2 reviews page HEADED,
injects session cookies, and:
  * captures the `reviews_and_filters` fragment response BODY (the real source)
  * waits for review cards (elv-stars) to render in the DOM
  * dumps buildId / embedded-JSON markers / a sample review
Bails cleanly if DataDome 403s the fragment, so the driver can retry.
"""
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.core.config import Config  # noqa: E402

OUT = ROOT / "data" / "probe"
OUT.mkdir(parents=True, exist_ok=True)

URL = "https://www.g2.com/products/databricks/reviews"
FRAG_RE = re.compile(r"reviews_and_filters", re.I)


def attempt(attempt_no, netlog, fragments):
    from patchright.sync_api import sync_playwright

    cfg = Config.load('config/default.yaml')
    cookies = json.loads((ROOT / "data" / "g2_cookies.json").read_text())
    ctx_cookies = [{"name": c["name"], "value": c["value"],
                    "domain": c["domain"], "path": "/"} for c in cookies]

    findings = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = browser.new_context(
            viewport={"width": cfg.browser.viewport["width"],
                      "height": cfg.browser.viewport["height"]},
            locale=cfg.browser.locale,
            timezone_id=cfg.browser.timezone,
            user_agent=cfg.browser.user_agent,
        )
        context.add_cookies(ctx_cookies)
        page = context.new_page()

        def on_response(resp):
            try:
                u = resp.url
                if "reviews_and_filters" in u:
                    ctype = resp.headers.get("content-type", "").split(";")[0]
                    rec = {"url": u, "status": resp.status, "content_type": ctype}
                    try:
                        body = resp.body().decode("utf-8", "replace")
                        rec["body_len"] = len(body)
                        if resp.status == 200:
                            fragments.setdefault(u, body)
                            rec["elv"] = body.count("elv-stars")
                            rec["markers"] = {m: (m in body) for m in
                                              ("reviewText", "reviewLikes", "starRating",
                                               "reviewRate", "reviewData", "reviewId",
                                               "__NEXT_DATA__", "self.__next_f")}
                    except Exception as e:
                        rec["body_err"] = str(e)
                    netlog.append(rec)
            except Exception:
                pass

        page.on("response", on_response)

        try:
            findings["egress_ip"] = page.evaluate(
                "fetch('https://api.ipify.org?format=json').then(r=>r.json()).then(j=>j.ip)")
        except Exception as e:
            findings["egress_ip_err"] = str(e)

        page.goto(URL, wait_until="load")
        findings["url_after_goto"] = page.url
        findings["title"] = page.title()

        # wait for review cards + collect html
        html = ""
        rendered = None
        for i in range(25):
            time.sleep(1)
            try:
                page.mouse.wheel(0, 1200)
            except Exception:
                pass
            html = page.content()
            if html.count("elv-stars") > 0:
                rendered = "elv-stars"
                break
        if not rendered:
            html = page.content()
        findings["review_rendered_marker"] = rendered
        findings["final_elv_stars"] = html.count("elv-stars")
        findings["final_bytes"] = len(html)

        # __NEXT_DATA__ buildId
        try:
            findings["buildId"] = page.evaluate(
                "()=>{const n=document.getElementById('__NEXT_DATA__');"
                "return n?JSON.parse(n.textContent).buildId:null}")
        except Exception as e:
            findings["buildId_err"] = str(e)

        dom_markers = {m: (m in html) for m in
                       ("__NEXT_DATA__", "self.__next_f", "<script type=\"application/json\"",
                        "reviewText", "reviewLikes", "starRating", "reviewRate")}
        findings["dom_markers"] = dom_markers
        findings["has_datadome_cookie"] = "datadome" in [c["name"] for c in context.cookies()]

        # save this attempt's rendered html (last writer wins; rv1 if first)
        if rendered or not (OUT / "g2_databricks_rendered.html").exists():
            (OUT / "g2_databricks_rendered.html").write_text(html, encoding="utf-8")
        findings["attempt"] = attempt_no
        browser.close()
    return findings


def find_reviews(obj, path="root", depth=0):
    if depth > 9 or not isinstance(obj, (dict, list)):
        return None
    if isinstance(obj, list):
        start = obj
    else:
        start = {}
    items = obj.items() if isinstance(obj, dict) else enumerate(obj)
    for k, v in items:
        p = f"{path}.{k}"
        if isinstance(v, list) and v and isinstance(v[0], dict):
            keys = set(v[0].keys())
            if keys & {"reviewText", "reviewLikes", "starRating", "reviewRate",
                       "reviewData", "reviewId"}:
                return (p, [json.dumps(x, default=str)[:1500] for x in v[:3]])
        r = find_reviews(v, p, depth + 1)
        if r:
            return r
    return None


def extract_sample(html):
    # __NEXT_DATA__
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if m:
        try:
            r = find_reviews(json.loads(m.group(1)))
            if r:
                return {"source": "__NEXT_DATA__", "path": r[0], "sample": r[1]}
        except Exception:
            pass
    return None


if __name__ == "__main__":
    netlog, fragments = [], {}
    last = None
    for attempt_no in (1, 2, 3):
        print(f"=== attempt {attempt_no} ===", flush=True)
        try:
            last = attempt(attempt_no, netlog, fragments)
        except Exception as e:
            print(f"  err: {type(e).__name__}: {e}", flush=True)
            continue
        if fragments or last.get("review_rendered_marker") == "elv-stars":
            break
        print("  empty/blocked, retrying", flush=True)
        time.sleep(4)

    sample = None
    if (OUT / "g2_databricks_rendered.html").exists():
        sample = extract_sample((OUT / "g2_databricks_rendered.html").read_text(encoding="utf-8"))

    # save compact evidence
    ev = {"url": URL, "findings": last, "fragments_saved":
          {u: {"bytes": len(b), "elv": b.count("elv-stars")} for u, b in fragments.items()},
          "network": netlog}
    if sample:
        ev["sample"] = sample
    (OUT / "g2_databricks_probe.json").write_text(
        json.dumps(ev, indent=2, default=str)[:1_500_000], encoding="utf-8")

    # sample review from a fragment body
    fr_sample = None
    for u, b in fragments.items():
        for m in re.finditer(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', b, re.S):
            try:
                r = find_reviews(json.loads(m.group(1)))
                if r:
                    fr_sample = {"url": u, "path": r[0], "sample": r[1]}
            except Exception:
                pass
        if fr_sample:
            break

    print(json.dumps({k: last.get(k) for k in
                      ["attempt", "egress_ip", "url_after_goto", "title",
                       "review_rendered_marker", "final_elv_stars", "final_bytes",
                       "buildId", "dom_markers", "has_datadome_cookie"]}, indent=2))
    print("fragment responses:")
    for rec in netlog:
        print(f"  {rec.get('status')} len={rec.get('body_len')} elv={rec.get('elv')} {rec['url'][:90]}")
    print("\n__NEXT_DATA__ sample found:", bool(sample or fr_sample))
