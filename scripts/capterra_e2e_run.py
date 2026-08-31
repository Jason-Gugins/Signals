"""Live validation for the Capterra scraper (PACED: 2 requests, 5s+ gap).

Fetches https://www.capterra.com/p/19319/JIRA/reviews/ (+ ?page=2) through
CurlCffiFetcher, runs extract_capterra_reviews on each body, then upserts
page-1 reviews into a TEMP database (never data/signals.db).
"""
import os, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.curl_fetcher import CurlCffiFetcher
from src.core.config import Config
from src.sources.marketplace.capterra import extract_capterra_reviews

BASE = "https://www.capterra.com/p/19319/JIRA/reviews/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def main() -> None:
    cfg = Config.load("config/default.yaml")
    fetcher = CurlCffiFetcher(UA)

    for page in (1, 2):
        url = BASE if page == 1 else f"{BASE}?page=2"
        print(f"\n=== page {page}: {url} ===")
        try:
            resp = fetcher.get(url)
        except Exception as e:
            print(f"  FETCH ERROR: {e}")
            break
        body_text = resp.body.decode("utf-8", "replace")
        print(f"  status={resp.status} | {len(resp.body)} bytes")
        if resp.status != 200:
            print(f"  head: {body_text[:200]!r}")
            break

        reviews = extract_capterra_reviews(body_text, "jira")
        print(f"  >>> {len(reviews)} REVIEWS")
        for r in reviews[:3]:
            print(f"     - {r.reviewer_name} | {r.rating}/5 | {r.posted_at} | {(r.review_title or '')[:45]}")

        if page == 1 and reviews:
            _verify_upsert(reviews)

        if page == 1:
            print("\n  ...sleeping 6s before page 2...")
            time.sleep(6)


def _verify_upsert(reviews) -> None:
    tmp_db = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "tmp", "capterra_e2e_tmp.db")
    os.makedirs(os.path.dirname(tmp_db), exist_ok=True)
    if os.path.exists(tmp_db):
        os.remove(tmp_db)
    from src.core.db import Database
    from src.sources.marketplace.collector import upsert_capterra_reviews
    db = Database(tmp_db)
    try:
        new, updated = upsert_capterra_reviews(db, reviews, now="2026-08-30T00:00:00Z")
        n = db.conn.execute(
            "SELECT COUNT(*) FROM g2_reviews WHERE source='capterra'").fetchone()[0]
        sample = db.conn.execute(
            "SELECT review_id, review_source, rating, reviewer_name FROM g2_reviews "
            "WHERE review_source='capterra' LIMIT 2").fetchall()
        print(f"\n=== upsert into TEMP DB ({tmp_db}) ===")
        print(f"  new={new} updated={updated} | rows with review_source='capterra': {n}")
        for row in sample:
            print(f"     - {row['review_id'][:20]}... | {row['review_source']} | {row['rating']}/5 | {row['reviewer_name']}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
