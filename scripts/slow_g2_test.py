"""Slow, careful G2 reviews test run — 3 fresh companies.
Single headed browser, long warm-up, generous delays, gentle scroll.
Goal: see whether reviews parse from the elv-* DOM after DataDome cool-down.
"""
import json, os, sys, time, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pathlib import Path
from patchright.sync_api import sync_playwright
from src.core.config import Config

cfg = Config.load("config/default.yaml")

cookies = json.loads(Path("data/g2_cookies.json").read_text())
cookies_for_ctx = [{"name": c["name"], "value": c["value"], "domain": c["domain"], "path": "/"} for c in cookies]

# 3 fresh companies not hammered in prior runs
COMPANIES = [
    ("Sierra", "sierra"),
    ("Helcim", "helcim"),
    ("AlphaSense", "alpha-sense"),
]

def main():
    p = sync_playwright().start()
    browser = p.chromium.launch(
        headless=False,
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
    ctx = browser.new_context(
        user_agent=cfg.browser.user_agent,
        viewport={"width": 1440, "height": 900},
        locale="en-US", timezone_id="America/Toronto")
    ctx.add_cookies(cookies_for_ctx)
    page = ctx.new_page()

    # LONG warm-up on homepage like a real user
    print("=== Warm-up on G2 homepage (slow) ===")
    try:
        page.goto("https://www.g2.com/", wait_until="domcontentloaded", timeout=30000)
        time.sleep(8)                     # initial dwell
        page.mouse.move(400, 300); time.sleep(1.5)
        page.mouse.wheel(0, 300); time.sleep(2)
        page.mouse.wheel(0, 400); time.sleep(2)
        print("  warm-up done")
    except Exception as e:
        print("  warmup err:", e)

    for i, (name, slug) in enumerate(COMPANIES):
        if i > 0:
            # cool-down between companies (real-human pause)
            print(f"  ...cooling down {25}s before next...")
            time.sleep(25)
        print(f"\n{'='*60}\n[{i+1}/{len(COMPANIES)}] {name}  ({slug})")

        page_url = f"https://www.g2.com/products/{slug}/reviews"
        frag_url = f"https://www.g2.com/products/{slug}/reviews_and_filters"

        # 1) slow navigate to reviews page
        page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(6)
        # gentle scroll
        page.mouse.move(500, 400); time.sleep(1)
        page.mouse.wheel(0, 500); time.sleep(2)
        page.mouse.wheel(0, 600); time.sleep(2)

        html_page = page.content()
        blocked = ("captcha-delivery.com" in html_page) or ('var dd=' in html_page)
        print(f"  reviews page: {len(html_page)} bytes, blocked={blocked}")

        # 2) gentle navigate to the fragment (the review container)
        time.sleep(3)
        try:
            page.goto(frag_url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(6)
            page.mouse.move(450, 350); time.sleep(1)
            page.mouse.wheel(0, 700); time.sleep(2)
            page.mouse.wheel(0, 700); time.sleep(2)
        except Exception as e:
            print("  frag nav err:", e)

        frag_html = page.content()
        frag_blocked = ("captcha-delivery.com" in frag_html) or ('var dd=' in frag_html)
        stars = frag_html.count("elv-stars")
        print(f"  fragment: {len(frag_html)} bytes, blocked={frag_blocked}, elv-stars={stars}")

        if not frag_blocked and stars > 0:
            # save this real fragment as the fixture candidate
            outf = Path("data/probe") / f"g2_{slug}_reviews_and_filters.html"
            outf.write_text(frag_html, encoding="utf-8")
            print(f"  >>> SAVED real fragment -> {outf}")

            # try the current parser
            from src.sources.marketplace.g2 import parse_g2_reviews
            reviews = parse_g2_reviews(frag_html, page_url)
            print(f"  current parser found {len(reviews)} reviews")
            # probe the elv-* structure around a star-rating
            idx = frag_html.find("elv-stars")
            if idx > 0:
                print("  DOM sample around first rating:")
                print("   ", frag_html[max(0,idx-150):idx+250].replace("\n"," ")[:350])
        else:
            print(f"  block/interstitial: {frag_html[:200]}" if frag_blocked else "  no stars in fragment")

        # save raw page too (small-ish)
        (Path("data/probe") / f"g2_{slug}_page.html").write_text(html_page[:200000], encoding="utf-8")

    browser.close(); p.stop()
    print("\n=== DONE ===")

if __name__ == "__main__":
    main()
