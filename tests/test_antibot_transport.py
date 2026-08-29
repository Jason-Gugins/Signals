"""Task 3 transport tests: conditional revalidation (304 -> cached body).

Network-free: the native engine is faked via monkeypatched _engine().
"""

import base64
import json

import pytest

from src.antibot.python.temporal import CacheEntry, RevalidationCache
from src.antibot.python.transport import AntibotResponse, SignalsTransport


class FakeEngine:
    """Minimal fake of the native signals_antibot module.

    Returns scripted responses and records the extra headers it was given so
    tests can assert validators were sent.
    """

    def __init__(self, script):
        # script: list of dicts {"status": int, "headers": {..}, "body": bytes}
        self.script = list(script)
        self.calls = []  # (url, extra_headers_list)

    def fetch_h2(self, url, extra):
        self.calls.append((url, extra))
        if not self.script:
            raise RuntimeError("fake engine script exhausted")
        step = self.script.pop(0)
        payload = {
            "status": step["status"],
            "headers": [[k, v] for k, v in step["headers"].items()],
            "body": "",
            "body_b64": base64.b64encode(step["body"]).decode(),
            "body_len": len(step["body"]),
            "resumed_session": step.get("resumed_session", False),
            "reused_connection": step.get("reused_connection", False),
        }
        return json.dumps(payload)


def _resp(status, headers=None, body=b"", **kw):
    return AntibotResponse(
        status=status, body=body, headers=headers or {}, **kw
    )


# ---------------------------------------------------------------------------
# RevalidationCache unit tests (no network, no engine)
# ---------------------------------------------------------------------------

def test_cache_conditional_headers_from_etag():
    c = RevalidationCache()
    assert c.conditional_headers("https://x/") == {}
    c.on_response("https://x/", _resp(200, {"etag": '"v1"'}, b"body"))
    assert c.conditional_headers("https://x/") == {"if-none-match": '"v1"'}


def test_cache_conditional_headers_from_last_modified():
    c = RevalidationCache()
    c.on_response("https://x/", _resp(200, {"last-modified": "Wed, 26 Aug 2026 00:00:00 GMT"}, b""))
    assert c.conditional_headers("https://x/") == {
        "if-modified-since": "Wed, 26 Aug 2026 00:00:00 GMT"
    }


def test_cache_serve_304_returns_cached_body_with_from_cache():
    c = RevalidationCache()
    c.on_response("https://x/", _resp(200, {"etag": '"v1"'}, b"original"))
    out = c.serve_304("https://x/", _resp(304, {"via-note": "n/a"}, b""))
    assert out.status == 200
    assert out.body == b"original"
    assert out.from_cache is True


def test_cache_serve_304_refreshes_validators():
    c = RevalidationCache()
    c.on_response("https://x/", _resp(200, {"etag": '"v1"'}, b"original"))
    out = c.serve_304("https://x/", _resp(304, {"etag": '"v2"'}, b""))
    assert out.headers["etag"] == '"v2"'
    # next conditional round uses the refreshed validator
    assert c.conditional_headers("https://x/") == {"if-none-match": '"v2"'}


def test_cache_skips_no_store_and_validatorless_responses():
    c = RevalidationCache()
    c.on_response("https://a/", _resp(200, {"cache-control": "no-store", "etag": '"x"'}, b""))
    c.on_response("https://b/", _resp(200, {}, b""))  # no etag/last-modified
    assert len(c) == 0
    assert not c.has_validators("https://a/")
    assert not c.has_validators("https://b/")


def test_cache_ignores_non_2xx_and_304_updates_nothing_without_entry():
    c = RevalidationCache()
    c.on_response("https://x/", _resp(404, {"etag": '"x"'}, b""))
    assert len(c) == 0
    with pytest.raises(KeyError):
        c.serve_304("https://x/", _resp(304))


def test_cache_entry_direct_and_clear():
    c = RevalidationCache()
    c._entries["https://x/"] = CacheEntry(status=200, body=b"z", etag='"e"')
    assert c.has_validators("https://x/")
    c.clear()
    assert len(c) == 0


# ---------------------------------------------------------------------------
# Transport-level conditional revalidation (fake engine, no network)
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_engine(monkeypatch):
    """Patch the module-level engine loader to return a FakeEngine."""
    holder = {}

    def install(script):
        eng = FakeEngine(script)
        holder["engine"] = eng
        monkeypatch.setattr(
            "src.antibot.python.transport._engine", lambda: eng
        )
        return eng

    return install


def test_second_fetch_sends_validators_and_serves_cached_body(fake_engine):
    eng = fake_engine([
        {"status": 200, "headers": {"etag": '"abc123"'}, "body": b"page-v1"},
        {"status": 304, "headers": {}, "body": b""},
    ])
    t = SignalsTransport()
    t.allow_fallback = False
    r1 = t.fetch("https://example.com/page")
    assert r1.status == 200 and r1.body == b"page-v1"
    assert r1.from_cache is False

    r2 = t.fetch("https://example.com/page")
    # validators were attached to the second request
    second_extra = dict(eng.calls[1][1])
    assert second_extra["if-none-match"] == '"abc123"'
    # 304 -> cached body, from_cache
    assert r2.status == 200
    assert r2.body == b"page-v1"
    assert r2.from_cache is True


def test_conditional_disabled_sends_no_validators(fake_engine):
    eng = fake_engine([
        {"status": 200, "headers": {"etag": '"v1"'}, "body": b"x"},
        {"status": 200, "headers": {"etag": '"v2"'}, "body": b"y"},
    ])
    t = SignalsTransport(conditional=False)
    t.allow_fallback = False
    t.fetch("https://example.com/page")
    t.fetch("https://example.com/page")
    assert all(
        not any(n.lower().startswith("if-") for n, _ in extra)
        for _, extra in eng.calls
    )


def test_temporal_flags_surface_from_engine_json(fake_engine):
    eng = fake_engine([
        {"status": 200, "headers": {}, "body": b"x", "reused_connection": True},
        {"status": 200, "headers": {}, "body": b"x", "resumed_session": True},
    ])
    t = SignalsTransport()
    t.allow_fallback = False
    r1 = t.fetch("https://example.com/a")
    r2 = t.fetch("https://example.com/b")
    assert r1.reused_connection is True
    assert r2.resumed_session is True


def test_no_validators_means_plain_refetch(fake_engine):
    eng = fake_engine([
        {"status": 200, "headers": {}, "body": b"x"},
        {"status": 200, "headers": {}, "body": b"y"},
    ])
    t = SignalsTransport()
    t.allow_fallback = False
    t.fetch("https://example.com/")
    r2 = t.fetch("https://example.com/")
    # no etag/last-modified on first response -> second request has no validators
    assert not any(
        n.lower() in ("if-none-match", "if-modified-since") for n, _ in eng.calls[1][1]
    )
    assert r2.from_cache is False
