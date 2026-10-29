"""End-to-end G2 run: 3 fresh companies via the production pipeline path.

Uses the exact runner code path: _fetch_g2_fragment -> PatchrightBrowserFetcher
(forced headed + warm-up) -> rendered fragment -> extract_g2_reviews.
"""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pathlib import Path
from src.core.config import Config
from src.core.patchright_browser import PatchrightBrowserFetcher
from src.sources.marketplace.g2 import g2_reviews_fragment_url, extract_g2_reviews

COMPANIES = [
    ("Harmonic", "harmonic"),
    ("Neo Security", "neosecurity"),
    ("Generalist AI", "generalist"),
]


class _Shim:
    def put(self, *a, **k):
        return None


def main():
    cfg = Config.load("config/default.yaml")
    total_reviews = 0

    for i, (name, slug) in enumerate(COMPANIES):
        if i > 0:
            print(f"\n  ...long cool-down 45s...")
            time.sleep(45)
        print(f"\n{'='*64}\n[{i+1}/{len(COMPANIES)}] {name}  ({slug})")

        browser = PatchrightBrowserFetcher(cfg, _Shim())
        url = g2_reviews_fragment_url(slug, page=None)
        # mirror the runner: force headed + warm-up
        cfg.browser.headless = False
        t0 = time.time()
        try:
            result = browser.fetch(
                url, source="marketplace_g2", domain="g2.com",
                wait_ms=4000, scroll=True,
                warmup_url="https://www.g2.com/", warmup_ms=4000,
            )
        except Exception as e:
            print(f"  ERROR: {e}")
            browser.close()
            continue
        elapsed = time.time() - t0

        body = result.doc.body if (result.ok and result.doc) else b""
        blocked = b"captcha-delivery" in body or b"var dd=" in body
        stars = body.count(b"elv-stars")
        print(f"  status={result.status} | {len(body)} bytes | {elapsed:.1f}s | blocked={blocked} | elv-stars={stars}")

        if not blocked and stars > 0:
            reviews = extract_g2_reviews(body.decode("utf-8", "replace"), slug)
            print(f"  >>> {len(reviews)} REVIEWS")
            for r in reviews[:3]:
                print(f"     - {r.reviewer_name} | {r.rating}/5 | {r.posted_at} | {(r.review_title or '')[:45]}")
            total_reviews += len(reviews)
            Path(f"data/probe/g2_{slug}_e2e.html").write_bytes(body)
        else:
            print(f"  blocked/empty: {body[:120]}")
        try:
            browser.close()
        except Exception:
            pass

    print(f"\n=== DONE — total reviews extracted: {total_reviews} ===")


if __name__ == "__main__":
    main()