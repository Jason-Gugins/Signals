"""Slow, careful Capterra discovery probe — ONE product (Jira reviews).

Headed Patchright, config UA, no cookie injection. Determines:
  - anti-bot stack (Cloudflare vs DataDome markers)
  - whether headed clears the challenge
  - server-rendered vs client-rendered reviews
  - review-card DOM selectors + one sample card (4KB)
  - pagination mechanism
  - cookie-consent gate selector
Max 3 attempts, then stops honestly. Never hammers.
"""
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from patchright.sync_api import sync_playwright
from src.core.config import Config

cfg = Config.load("config/default.yaml")

TARGET = "https://www.capterra.com/p/19319/JIRA/reviews/"
OUT_DIR = Path("data/probe")
OUT_DIR.mkdir(parents=True, exist_ok=True)

CF_MARKERS = [
    ("Just a moment", "body-text"),
    ("cf-mitigated", "header/text"),
    ("cf_chl_opt", "js-var"),
    ("__cf_bm", "cookie"),
    ("challenge-platform", "js-path"),
    ("cf-chl", "attr"),
]
DD_MARKERS = [
    ("captcha-delivery.com", "script-src"),
    ("var dd=", "js-var"),
    ("datadome", "cookie"),
    ("DataDome", "brand"),
    ("geo.captcha-delivery.com", "iframe"),
]


def _scan(html: str, cookies) -> dict:
    low = html.lower()
    cf = [name for name, _kind in CF_MARKERS if name.lower() in low]
    dd = [name for name, _kind in DD_MARKERS if name.lower() in low]
    cookie_names = {c["name"].lower() for c in cookies}
    if "__cf_bm" in cookie_names:
        cf.append("__cf_bm(cookie-jar)")
    if any(n.startswith("datadome") for n in cookie_names):
        dd.append("datadome(cookie-jar)")
    return {"cf": sorted(set(cf)), "dd": sorted(set(dd))}


def attempt(n: int) -> dict:
    print(f"\n=== ATTEMPT {n}/3 ===")
    result = {"attempt": n}
    p = sync_playwright().start()
    try:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        ctx = browser.new_context(
            user_agent=cfg.browser.user_agent,
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            timezone_id=cfg.browser.timezone or "America/Toronto",
        )
        page = ctx.new_page()
        statuses = []
        page.on("response", lambda r: statuses.append(
            {"url": r.url[:120], "status": r.status}) if r.url.startswith("https://www.capterra.com") else None)

        # Warm-up on homepage like a real user
        print("warm-up: capterra.com homepage")
        try:
            page.goto("https://www.capterra.com/", wait_until="domcontentloaded", timeout=45000)
            time.sleep(8)
            page.mouse.move(400, 300); time.sleep(1.5)
            page.mouse.wheel(0, 300); time.sleep(2)
            page.mouse.wheel(0, 400); time.sleep(2)
        except Exception as e:
            print("  warmup err:", e)

        print("navigate:", TARGET)
        resp = page.goto(TARGET, wait_until="domcontentloaded", timeout=60000)
        result["http_status"] = resp.status if resp else None
        time.sleep(8)
        for _ in range(3):  # gentle scroll
            page.mouse.move(500, 400); time.sleep(1)
            page.mouse.wheel(0, 500); time.sleep(2)

        result["final_url"] = page.url
        html = page.content()
        result["html_bytes"] = len(html)
        cookies = ctx.cookies("https://www.capterra.com")
        result["markers"] = _scan(html, cookies)

        # consent gate detection
        consent = {}
        for sel in ["#onetrust-banner-sdk", "#onetrust-consent-sdk", "#onetrust-button-group",
                    "#didomi-notice", ".didomi-popup", "#CybotCookiebotDialog",
                    "[id*='consent' i]", "[class*='consent' i]", "[aria-label*='cookie' i]"]:
            try:
                loc = page.locator(sel).first
                if loc.count() and loc.is_visible():
                    consent[sel] = True
            except Exception:
                pass
        result["consent_selectors"] = sorted(consent)

        # challenge title?
        result["title"] = page.title()[:120]

        # review-card markers in rendered DOM
        counts = {m: html.count(m) for m in
                  ["data-testid", "review-card", "ReviewCard", "class=\"review", "review-content",
                   "review-text", "typo-"] if m in html}
        result["marker_counts"] = counts

        # client-rendered? compare content before/after JS settle is implicit; check __NEXT_DATA__ etc.
        result["frameworks"] = [f for f in ["__NEXT_DATA__", "__NUXT__", "react-root", "id=\"root\"",
                                            "id=\"app\"", "window.__INITIAL"] if f in html]

        # pagination
        pag = sorted(set(re.findall(r"reviews/\?page=(\d+)", html)))[:10]
        result["pagination_page_links"] = pag
        pag2 = sorted(set(re.findall(r"href=\"([^\"]*page[=/][^\"]*)\"", html)))[:15]
        result["pagination_hrefs"] = pag2

        # save rendered HTML
        out = OUT_DIR / "capterra_jira_rendered.html"
        out.write_text(html, encoding="utf-8")
        result["saved"] = str(out)

        # blocked?
        blocked = bool(result["markers"]["cf"] or result["markers"]["dd"])
        result["blocked_markers"] = blocked
        result["statuses_head"] = statuses[:8]
        return result
    finally:
        try:
            browser.close()
        except Exception:
            pass
        p.stop()


def main():
    for n in (1, 2, 3):
        try:
            r = attempt(n)
        except Exception as e:
            r = {"attempt": n, "error": repr(e)[:300]}
        print(json.dumps(r, indent=2, default=str))
        (OUT_DIR / f"capterra_probe_attempt{n}.json").write_text(
            json.dumps(r, indent=2, default=str), encoding="utf-8")
        if r.get("error"):
            time.sleep(20)
            continue
        if not r.get("blocked_markers") and r.get("html_bytes", 0) > 50000:
            print(">>> cleared with real content — stopping (success)")
            break
        if n < 3:
            print(">>> challenged — cooling down 60s before next attempt")
            time.sleep(60)
    print("=== DONE ===")


if __name__ == "__main__":
    main()
