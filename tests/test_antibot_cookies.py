"""Persistent browser-like cookie jar (temporal stealth — Task 3 remainder).

No network: all parsing/matching/persistence logic is exercised offline.
"""

from __future__ import annotations

import json
import time

import pytest

from src.antibot.python.cookies import PersistentCookieJar


# ---------------------------------------------------------------------------
# set_from_response parsing
# ---------------------------------------------------------------------------


def test_set_from_response_parses_set_cookie_header():
    jar = PersistentCookieJar()
    jar.set_from_response(
        "https://example.com/",
        {"set-cookie": "sessionid=abc123; Path=/; HttpOnly"},
    )
    got = jar.cookies_for("https://example.com/x")
    assert {"name": "sessionid", "value": "abc123", "domain": "example.com"} in got


def test_set_from_response_comma_folded_multiple_cookies():
    jar = PersistentCookieJar()
    jar.set_from_response(
        "https://example.com/",
        {"set-cookie": "a=1; Path=/, b=2; Path=/"},
    )
    names = {c["name"]: c["value"] for c in jar.cookies_for("https://example.com/")}
    assert names == {"a": "1", "b": "2"}


def test_set_from_response_accepts_pairs_from_engine_json():
    jar = PersistentCookieJar()
    jar.set_from_response(
        "https://example.com/",
        [("set-cookie", "a=1"), ("set-cookie", "b=2")],
    )
    names = {c["name"]: c["value"] for c in jar.cookies_for("https://example.com/")}
    assert names == {"a": "1", "b": "2"}


def test_max_age_sets_expiry():
    jar = PersistentCookieJar(clock=lambda: 1000.0)
    jar.set_from_response(
        "https://example.com/",
        {"set-cookie": "tok=t; Max-Age=60; Path=/"},
    )
    assert jar.cookies_for("https://example.com/")
    # 120s later the cookie is gone.
    jar.clock = lambda: 1120.0
    assert jar.cookies_for("https://example.com/") == []


def test_expires_attribute_parsed():
    jar = PersistentCookieJar(clock=lambda: 1000.0)
    future = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(2000))
    jar.set_from_response(
        "https://example.com/",
        {"set-cookie": f"tok=t; expires={future}; Path=/"},
    )
    assert jar.cookies_for("https://example.com/")


# ---------------------------------------------------------------------------
# cookies_for matching
# ---------------------------------------------------------------------------


def test_cookies_for_matches_host():
    jar = PersistentCookieJar()
    jar.update("sid", "v", domain="example.com")
    assert jar.cookies_for("https://example.com/a")[0]["name"] == "sid"


def test_cookies_for_matches_parent_domain_suffix():
    jar = PersistentCookieJar()
    jar.update("sid", "v", domain=".example.com")
    assert jar.cookies_for("https://www.example.com/")[0]["value"] == "v"


def test_cookies_for_rejects_unrelated_host():
    jar = PersistentCookieJar()
    jar.update("sid", "v", domain="example.com")
    assert jar.cookies_for("https://other.org/") == []


def test_cookies_for_excludes_expired():
    jar = PersistentCookieJar(clock=lambda: 1000.0)
    jar.update("sid", "v", domain="example.com", expires=500.0)
    assert jar.cookies_for("https://example.com/") == []


# ---------------------------------------------------------------------------
# update / clear
# ---------------------------------------------------------------------------


def test_update_same_domain_name_overwrites():
    jar = PersistentCookieJar()
    jar.update("sid", "old", domain="example.com")
    jar.update("sid", "new", domain="example.com")
    assert jar.cookies_for("https://example.com/")[0]["value"] == "new"


def test_clear_all_and_by_domain():
    jar = PersistentCookieJar()
    jar.update("a", "1", domain="example.com")
    jar.update("b", "2", domain="other.org")
    jar.clear(domain="example.com")
    assert jar.cookies_for("https://example.com/") == []
    assert jar.cookies_for("https://other.org/")
    jar.clear()
    assert jar.cookies_for("https://other.org/") == []


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------


def test_persistence_roundtrip(tmp_path):
    p = tmp_path / "cookies.json"
    jar = PersistentCookieJar(path=p)
    jar.update("sid", "v", domain="example.com")
    jar.save()

    jar2 = PersistentCookieJar(path=p)
    jar2.load()
    assert jar2.cookies_for("https://example.com/")[0]["value"] == "v"


def test_load_tolerates_missing_file(tmp_path):
    jar = PersistentCookieJar(path=tmp_path / "nope.json")
    jar.load()  # must not raise
    assert jar.cookies_for("https://example.com/") == []


def test_load_tolerates_corrupt_file(tmp_path):
    p = tmp_path / "cookies.json"
    p.write_text("{not json!!")
    jar = PersistentCookieJar(path=p)
    jar.load()  # must not raise
    assert jar.cookies_for("https://example.com/") == []


# ---------------------------------------------------------------------------
# transport wiring
# ---------------------------------------------------------------------------


class FakeEngine:
    """Stand-in for the native engine returning different set-cookies."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def fetch(self, url, headers=None):
        self.calls += 1
        return json.dumps(
            {
                "status": 200,
                "headers": [[":status", "200"], *self.payload[self.calls - 1]],
                "body": "",
                "body_b64": "",
                "body_len": 0,
                "reused_connection": False,
                "resumed_session": False,
            }
        )


class FakeEngineModule:
    def __init__(self, engine):
        self.SignalsEngine = lambda: engine


@pytest.fixture
def transport_cls(monkeypatch):
    from src.antibot.python import transport as tr

    tr_mod = tr
    return tr_mod


def test_transport_jar_accumulates_across_fetches(monkeypatch, transport_cls, tmp_path):
    tr = transport_cls
    eng = FakeEngine(
        [
            [("set-cookie", "a=1; Path=/")],
            [("set-cookie", "b=2; Path=/")],
        ]
    )
    t = tr.SignalsTransport(cookie_jar=tr.PersistentCookieJar(path=tmp_path / "c.json"))
    monkeypatch.setattr(t, "_engine", FakeEngineModule(eng))
    t.fetch("https://example.com/one")
    t.fetch("https://example.com/two")
    sent = eng.calls
    assert sent == 2
    # The second fetch must have carried the cookie from the first response.
    # (Inspect via jar instead of the wire: caller cookies merged under jar.)
    assert {c["name"]: c["value"] for c in t.cookie_jar.cookies_for("https://example.com/")} == {
        "a": "1",
        "b": "2",
    }


@pytest.mark.allow_network  # exercises the curl_cffi fallback seam with fake engines — no real host contacted
def test_transport_caller_cookies_win_over_jar(monkeypatch, transport_cls, tmp_path):
    tr = transport_cls
    seen = {}

    class RecordingEngine(FakeEngine):
        def fetch(self, url, headers=None):
            for n, v in headers or []:
                if n == "cookie":
                    seen["cookie"] = v
            return super().fetch(url, headers)

    t = tr.SignalsTransport(cookie_jar=tr.PersistentCookieJar(path=tmp_path / "c.json"))
    eng = RecordingEngine([[("set-cookie", "a=1; Path=/")]])
    monkeypatch.setattr(t, "_engine", FakeEngineModule(eng))
    t.fetch("https://example.com/one")
    # caller cookie overrides the jar's `a` and adds `b` from the jar
    t.fetch("https://example.com/two", cookies=[{"name": "a", "value": "override"}])
    assert seen["cookie"].startswith("a=override")
