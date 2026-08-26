"""Try fetching reviews_and_filters fragment directly and read page.content().
Direct goto avoids the navigation-away race that broke response-body reads.
Tries a few companies; DataDome may still block some.
"""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pathlib import Path
from patchright.sync_api import sync_playwright
from src.core.config import Config

cfg = Config.load("config/default.yaml")
cookies = json.loads(Path("data/g2_cookies.json").read_text())
cookies_for_ctx = [{"name": c["name"], "value": c["value"], "domain": c["domain"], "path": "/"} for c in cookies]

SLUGS = ["mach","wispr","convex","helpjuice","monday","asana","notion","clickup"]

p = sync_playwright().start()
browser = p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled","--no-sandbox"])
ctx = browser.new_context(
    user_agent=cfg.browser.user_agent,
    viewport={"width":1440,"height":900},
    locale="en-US", timezone_id="America/Toronto")
ctx.add_cookies(cookies_for_ctx)
page = ctx.new_page()

out = Path("data/probe"); out.mkdir(parents=True, exist_ok=True)
results = {}

for slug in SLUGS:
    frag_url = f"https://www.g2.com/products/{slug}/reviews_and_filters"
    page_url = f"https://www.g2.com/products/{slug}/reviews"
    try:
        page.goto(page_url, wait_until="domcontentloaded", timeout=20000)
        time.sleep(3)
        page.goto(frag_url, wait_until="domcontentloaded", timeout=20000)
        time.sleep(4)
        html = page.content()
        is_block = ("captcha-delivery.com" in html) or ('var dd=' in html)
        has_stars = html.count("elv-stars") if html else 0
        has_review_text = ("review" in html.lower()) if html else False
        results[slug] = {"len": len(html or ""), "blocked": is_block, "elv_stars": has_stars, "has_review_term": has_review_text}
        print(f"{slug}: len={len(html or '')} blocked={is_block} elv_stars={has_stars} review_term={has_review_text}")
        if not is_block and html and ("elv-stars" in html or "review" in html.lower()):
            (out / "g2_fragment_content.html").write_text(html, encoding="utf-8")
            print(f"  >>> SAVED g2_fragment_content.html")
            break
    except Exception as e:
        print(f"{slug}: ERR {e}")

(Path("data/probe")/"g2_direct_fragments.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
browser.close(); p.stop()
print("DONE")
