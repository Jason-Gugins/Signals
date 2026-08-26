"""Focused one-shot capture of the G2 reviews_and_filters 200 fragment body.

Cycles over several company slugs (avoids DataDome per-page correlation from
hitting one page repeatedly). Launches ONE headed browser, visits each company's
reviews page + fragment, captures the first GENUINE g2 reviews_and_filters body
(not a captcha-delivery interstitial), saves it, exits. Bounded ~240s.
"""
import json, os, sys, time, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pathlib import Path
from patchright.sync_api import sync_playwright
from src.core.config import Config

cfg = Config.load("config/default.yaml")

cookies = json.loads(Path("data/g2_cookies.json").read_text())
cookies_for_ctx = [{"name": c["name"], "value": c["value"], "domain": c["domain"], "path": "/"} for c in cookies]

# diversify: a handful of funded-software companies, spread across categories
SLUGS = ["suno", "groq", "elevenlabs", "cyera", "decagon", "candid-health"]

REAL_BODIES = []   # list of (slug, url, body)

def wants(u):
    return "reviews_and_filters" in u and "captcha-delivery.com" not in u \
        and "geo.captcha" not in u and "signin" not in u

def run():
    p = sync_playwright().start()
    browser = p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
    ctx = browser.new_context(user_agent=cfg.browser.user_agent,
                              viewport={"width": 1440, "height": 900},
                              locale="en-US", timezone_id="America/Toronto")
    ctx.add_cookies(cookies_for_ctx)
    page = ctx.new_page()

    def on_fragment(resp):
        u = resp.url
        if wants(u):
            try:
                if resp.status == 200 and "text/html" in resp.headers.get("content-type", ""):
                    body = resp.text()
                    idx = len(REAL_BODIES)
                    REAL_BODIES.append(u)
                    print(f"SNAGGED real fragment {resp.status} len={len(body)} {u[:90]}")
                    (Path("data/probe") / f"g2_fragment_{idx}.html").write_text(body, encoding="utf-8")
            except Exception:
                pass

    page.on("response", on_fragment)

    # warm up on homepage once
    try:
        page.goto("https://www.g2.com/", wait_until="domcontentloaded", timeout=30000); time.sleep(3)
        page.mouse.wheel(0, 500); time.sleep(1)
    except Exception as e:
        print("warmup err:", e)

    for i, slug in enumerate(SLUGS):
        if REAL_BODIES:
            break
        target = f"https://www.g2.com/products/{slug}/reviews"
        frag = f"https://www.g2.com/products/{slug}/reviews_and_filters"
        print(f"\n--- [{i+1}/{len(SLUGS)}] {slug} ---")
        try:
            page.goto(target, wait_until="domcontentloaded", timeout=25000)
            time.sleep(5)
            for _ in range(4):
                page.mouse.wheel(0, 1500); time.sleep(1.0)
            # force fragment directly
            try:
                page.goto(frag, wait_until="domcontentloaded", timeout=20000)
                time.sleep(4)
                page.mouse.wheel(0, 1200); time.sleep(1)
            except Exception:
                pass
            # newest + page2 to trigger more fragment calls
            try:
                page.goto(target + "?sort=newest", wait_until="domcontentloaded", timeout=20000); time.sleep(3)
                page.mouse.wheel(0, 1000); time.sleep(1)
                page.goto(target + "?page=2", wait_until="domcontentloaded", timeout=20000); time.sleep(3)
                page.mouse.wheel(0, 1000); time.sleep(1)
            except Exception:
                pass
        except Exception as e:
            print("  err:", e)
            continue

    final = page.content()
    out_dir = Path("data/probe"); out_dir.mkdir(parents=True, exist_ok=True)

    result = {
        "egress_ip": "99.226.66.20",
        "real_fragments_captured": len(REAL_BODIES),
        "final_title": re.search(r"<title>(.*?)</title>", final or "").group(1) if final else None,
    }
    print("RESULT:", json.dumps(result))
    (out_dir / "g2_fragment_capture.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    (out_dir / "g2_fragment_final.html").write_text(final or "", encoding="utf-8")

    # list what got saved
    for f in sorted(out_dir.glob("g2_*fragment*.html")):
        print("SAVED:", f.name, f.stat().st_size, "bytes")

    browser.close(); p.stop()

if __name__ == "__main__":
    run()
