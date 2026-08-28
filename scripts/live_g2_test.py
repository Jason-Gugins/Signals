"""Live test of the Patchright stealth browser against G2 DataDome."""
import json, tempfile, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pathlib import Path
from src.core.config import Config
from src.core.db import Database, DataDomeCookieStore
from src.core.curl_fetcher import CurlCffiFetcher
from src.core.patchright_browser import PatchrightBrowserFetcher
from src.sources.techstack.datadome import is_datadome_challenge
from src.sources.marketplace.g2 import parse_g2_reviews


class RawStoreShim:
    def put(self, *a, **k):
        return None


cfg = Config.load('config/default.yaml')
print('DataDome enabled:', cfg.datadome.enabled)
print('Browser headless:', cfg.browser.headless)
print('Browser UA:', cfg.browser.user_agent[:40], '...')

# Load user's session cookies to inject into the stealth browser
cookie_path = 'data/g2_cookies.json'
browser_cookies = json.loads(Path(cookie_path).read_text())
cookies_for_ctx = [
    {'name': c['name'], 'value': c['value'], 'domain': c['domain'], 'path': '/'}
    for c in browser_cookies
]
print('Session cookies to inject:', [c['name'] for c in cookies_for_ctx])

db = Database(tempfile.mktemp(suffix='.db'))

companies = [('Databricks', 'databricks'), ('Ramp', 'ramp'), ('Suno', 'suno')]

browser = PatchrightBrowserFetcher(cfg, RawStoreShim())
browser.start()
print('Patchright browser started.\n')

for company_name, slug in companies:
    url = f'https://www.g2.com/products/{slug}/reviews'
    warmup = 'https://www.g2.com/'
    sep = '=' * 60
    print(sep)
    print(f'Company: {company_name}')
    print(f'URL: {url}')
    print(sep)

    try:
        result = browser.fetch(
            url, source='marketplace_g2', domain='g2.com',
            warmup_url=warmup, warmup_ms=4000,
            cookies=cookies_for_ctx,
            wait_ms=7000, scroll=True,
        )
        if result.ok and result.doc and result.doc.body:
            body = result.doc.body
            if is_datadome_challenge(status=result.status, body=body):
                print(f'  STILL BLOCKED by DataDome (status {result.status}, body {len(body)} bytes)')
            elif result.status == 200:
                reviews = parse_g2_reviews(body.decode('utf-8', 'replace'), url)
                print(f'  SUCCESS! {len(reviews)} reviews found')
                for r in reviews[:5]:
                    print(f'    - {r.reviewer_name} ({r.rating}/5): {r.review_title}')
                    if r.review_body:
                        print(f'      Body: {r.review_body[:100]}')
                    if r.pros:
                        print(f'      Pros: {r.pros[:2]}')
                print(f'    (+{max(len(reviews)-5,0)} more)')
                dd = [c for c in (result.cloudflare_cookies or []) if c.get('name') == 'datadome']
                print(f'  datadome cookie present: {len(dd) > 0}')
            else:
                print(f'  Status {result.status}, not DataDome, not 200. Body: {body[:200]}')
        else:
            print(f'  Fetch failed: ok={result.ok} status={result.status} err={result.error}')
    except Exception as e:
        print(f'  ERROR: {type(e).__name__}: {e}')
    print()

browser.close()
print('Done.')
