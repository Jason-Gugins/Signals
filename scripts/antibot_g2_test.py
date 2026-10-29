"""Anti-bot tier test: 3 fresh G2 companies, slow pacing.

Tier-1 = SignalsTransport (real Chrome TLS + own h2 + temporal stealth).
If DataDome challenges, PatchrightGhost solves and we bounce.
RouteState tracks per-domain cookies (Warm on revisit).
"""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pathlib import Path
from src.core.config import Config
from src.antibot.python.transport import SignalsTransport
from src.antibot.python.ghost import solve_and_bounce, PatchrightGhost, extract_clearance_cookies
from src.antibot.python.routing import RouteState
from src.sources.marketplace.g2 import extract_g2_reviews

COMPANIES = [
    ("Harmonic", "harmonic"),
    ("Neo Security", "neosecurity"),
    ("Generalist AI", "generalist"),
]

def main():
    cfg = Config.load("config/default.yaml")
    transport = SignalsTransport(user_agent=cfg.browser.user_agent)
    print("=== Tier-1: native Chrome-TLS engine ===")
    print("  engine available:", transport.engine_available)

    routing = RouteState(path="data/antibot/routing_test.json")
    session_cookies = json.loads(Path("data/g2_cookies.json").read_text())
    clearance = extract_clearance_cookies(session_cookies)
    print("  clearance cookies from session:", [c["name"] for c in clearance])
    # passed per-fetch below (transport supports cookies= kwarg)

    for i, (name, slug) in enumerate(COMPANIES):
        if i > 0:
            print(f"\n  ...long cool-down 45s...")
            time.sleep(45)
        print(f"\n{'='*64}\n[{i+1}/{len(COMPANIES)}] {name}  ({slug})  route={routing.decide('g2.com')}")
        url = f"https://www.g2.com/products/{slug}/reviews_and_filters"

        t0 = time.time()
        try:
            from src.core.patchright_browser import PatchrightBrowserFetcher

            class _Shim:
                def put(self, *a, **k):
                    return None

            fetcher = PatchrightBrowserFetcher(cfg, _Shim())
            ghost = PatchrightGhost(fetcher)
            # a tiny wrapper transport that always carries the session clearance cookies
            class SessionTransport:
                def __init__(self, inner, cookies):
                    self._inner, self._cookies = inner, cookies
                    self.engine_available = inner.engine_available
                def fetch(self, url, **kw):
                    kw.setdefault("cookies", self._cookies)
                    return self._inner.fetch(url, **kw)

            bounce = solve_and_bounce(
                SessionTransport(transport, clearance), ghost, url,
            )
        except Exception as e:
            print(f"  ERROR: {type(e).__name__}: {e}")
            continue
        elapsed = time.time() - t0

        via = bounce.via
        resp = bounce.response
        if resp is None:
            print(f"  via={via} | solve failed | {elapsed:.1f}s")
            continue

        body = resp.body or b""
        dd = b"captcha-delivery" in body or b"var dd=" in body
        stars = body.count(b"elv-stars")
        articles = body.count(b'track-in-viewport')
        print(f"  via={via} | status={resp.status} | {len(body)} bytes | {elapsed:.1f}s")
        print(f"  blocked={dd} | elv-stars={stars} | review-cards~{articles}")
        print(f"  cached={resp.from_cache} reused={resp.reused_connection} resumed={resp.resumed_session}")

        if resp.cookies:
            print(f"  cookies from tier-1: {list(resp.cookies.keys())[:6]}")

        if not dd and stars > 0:
            slug_ = slug
            reviews = extract_g2_reviews(body.decode("utf-8", "replace"), slug_)
            print(f"  >>> {len(reviews)} REVIEWS EXTRACTED")
            for r in reviews[:3]:
                print(f"     - {r.reviewer_name} | {r.rating}/5 | {r.posted_at} | {(r.review_title or '')[:45]}")
            if reviews:
                routing.record_solve("g2.com", cookies=["datadome"])
                outf = Path("data/probe") / f"g2_{slug}_antibot.html"
                outf.write_bytes(body)
                print(f"  >>> SAVED {outf}")
        elif dd:
            print("  challenge still present after bounce")

    print("\n=== DONE ===")

if __name__ == "__main__":
    main()
