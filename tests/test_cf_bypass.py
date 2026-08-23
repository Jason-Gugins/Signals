from types import SimpleNamespace
from src.sources.techstack.cf_bypass import CloudflareBypass, BypassOutcome


class _StubHttp:
    """Stub HttpFetcher. Returns challenge on first get, 200 on replay_with_cookie."""
    def __init__(self, challenge_body=b"<html><title>Just a moment...</title></html>",
                 replay_ok=True, replay_body=b"<html>real</html>"):
        self.challenge_body = challenge_body
        self.replay_ok = replay_ok
        self.replay_body = replay_body
        self.get_calls = 0
    def get(self, task, **kw):
        self.get_calls += 1
        from src.core.http import FetchResult
        from src.core.models import Document
        doc = Document(doc_id="d", source="techstack", url=task.url, body=self.challenge_body)
        return FetchResult(True, 403, doc, False, None, 1, [])
    def replay(self, url, *, cookies, user_agent):
        from src.core.http import FetchResult
        from src.core.models import Document
        if self.replay_ok:
            doc = Document(doc_id="d2", source="techstack", url=url, body=self.replay_body)
            return FetchResult(True, 200, doc, False, None, 1, [])
        doc = Document(doc_id="d3", source="techstack", url=url, body=self.challenge_body)
        return FetchResult(True, 403, doc, False, None, 1, [])


class _StubBrowser:
    """Stub BrowserFetcher. capture_html returns cleared body + cf cookies."""
    def __init__(self, cleared_body=b"<html><script src='https://js.hs-scripts.com/x.js'></script></html>",
                 cf_cookies=None, ok=True):
        self.cleared_body = cleared_body
        self.cf_cookies = cf_cookies or [{"name": "cf_clearance", "value": "tok", "domain": "acme.com"},
                                          {"name": "__cf_bm", "value": "bm1", "domain": ".acme.com"}]
        self.ok = ok
    def fetch(self, url, *, source, domain, capture_html=False, **kw):
        from src.core.http import FetchResult
        from src.core.models import Document
        if not self.ok:
            return FetchResult(False, 0, None, False, "browser failed", 1, [])
        doc = Document(doc_id="b", source=source, url=url, body=self.cleared_body)
        return FetchResult(True, 200, doc, False, None, 1, self.cf_cookies)


def test_bypass_uses_cached_cookie_then_http_200(tmp_path):
    from src.core.config import Config
    from src.core.db import Database, CfCookieStore
    cfg = Config()
    db = Database(tmp_path / "s.db")
    cookie_store = CfCookieStore(db)
    # pre-seed a cached cookie
    cookie_store.put("acme.com", user_agent="UA", proxy="direct",
                     cookies=[{"name": "cf_clearance", "value": "cached"}],
                     expires_at="2026-12-31T00:00:00+00:00")
    http = _StubHttp(replay_ok=True)
    browser = _StubBrowser()
    bypass = CloudflareBypass(cfg, cookie_store, http, browser)
    outcome = bypass.attempt(domain="acme.com", url="https://acme.com/", user_agent="UA")
    assert outcome.success
    assert outcome.method == "cookie_reuse"


def test_bypass_browser_solve_persists_cookie(tmp_path):
    from src.core.config import Config
    from src.core.db import Database, CfCookieStore
    cfg = Config()
    db = Database(tmp_path / "s.db")
    cookie_store = CfCookieStore(db)
    http = _StubHttp(replay_ok=False)  # cookie reuse fails → fall to browser
    browser = _StubBrowser()
    bypass = CloudflareBypass(cfg, cookie_store, http, browser)
    outcome = bypass.attempt(domain="acme.com", url="https://acme.com/", user_agent="UA")
    assert outcome.success
    assert outcome.method == "browser"
    # cookie persisted
    row = cookie_store.get("acme.com", user_agent="UA", proxy="direct")
    assert row is not None


def test_bypass_hard_stop_records_cloudflare_only(tmp_path):
    from src.core.config import Config
    from src.core.db import Database, CfCookieStore
    cfg = Config()
    cfg.cloudflare.solver_provider = None  # no solver
    db = Database(tmp_path / "s.db")
    cookie_store = CfCookieStore(db)
    http = _StubHttp(replay_ok=False)
    browser = _StubBrowser(ok=False)  # browser fails
    bypass = CloudflareBypass(cfg, cookie_store, http, browser)
    outcome = bypass.attempt(domain="acme.com", url="https://acme.com/", user_agent="UA")
    assert outcome.success is False
    assert outcome.challenge_type in ("managed", "js")
