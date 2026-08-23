from pathlib import Path
from src.core.config import Config
from src.core.db import Database, CfCookieStore
from src.sources.techstack.cf_bypass import CloudflareBypass
from src.sources.techstack.collector import TechstackSource
from src.core.models import Account, Document


class _StubHttp:
    def __init__(self, challenge_body, replay_ok=True, replay_body=b""):
        self.challenge_body = challenge_body
        self.replay_ok = replay_ok
        self.replay_body = replay_body
    def get(self, task, **kw):
        from src.core.http import FetchResult
        doc = Document(doc_id="d", source="techstack", url=task.url, body=self.challenge_body)
        return FetchResult(True, 403, doc, False, None, 1, [])
    def replay(self, url, *, cookies, user_agent):
        from src.core.http import FetchResult
        if self.replay_ok:
            doc = Document(doc_id="d2", source="techstack", url=url, body=self.replay_body)
            return FetchResult(True, 200, doc, False, None, 1, [])
        doc = Document(doc_id="d3", source="techstack", url=url, body=self.challenge_body)
        return FetchResult(True, 403, doc, False, None, 1, [])


class _StubBrowser:
    def __init__(self, cleared_body, cf_cookies):
        self.cleared_body = cleared_body
        self.cf_cookies = cf_cookies
    def fetch(self, url, *, source, domain, capture_html=False, **kw):
        from src.core.http import FetchResult
        doc = Document(doc_id="b", source=source, url=url, body=self.cleared_body)
        return FetchResult(True, 200, doc, False, None, 1, self.cf_cookies)


def test_bypass_waterfall_end_to_end_offline(tmp_path):
    challenge = Path("tests/fixtures/techstack/challenge_cloudflare.html").read_bytes()
    cleared = Path("tests/fixtures/techstack/challenge_cleared_acme.stub.html").read_bytes()
    cfg = Config()
    db = Database(tmp_path / "s.db")
    cookie_store = CfCookieStore(db)
    http = _StubHttp(challenge_body=challenge, replay_ok=False)  # no cached cookie
    browser = _StubBrowser(
        cleared_body=cleared,
        cf_cookies=[{"name": "cf_clearance", "value": "tok", "domain": "acme.com"},
                    {"name": "__cf_bm", "value": "bm1", "domain": ".acme.com"}],
    )
    bypass = CloudflareBypass(cfg, cookie_store, http, browser)
    outcome = bypass.attempt(domain="acme.com", url="https://acme.com/", user_agent="UA")
    assert outcome.success
    assert outcome.method == "browser"
    # parse the cleared body → real vendors
    src = TechstackSource()
    matches = src.harvest_tech(
        Document(doc_id="h", source="techstack", url="https://acme.com/", body=cleared),
        Account(domain="acme.com"), {"today": "2026-08-23"})
    vendors = {m.vendor for m in matches}
    assert "hubspot" in vendors  # parsed from cleared fixture (frozen needle)
