"""Offline tests for the fetcher-based Google News token decoder."""
import json

from src.sources.news.decode import (
    decode_token,
    extract_token,
    fetch_decoding_params,
    post_decode,
)


PARAMS_HTML = (
    b'<html><c-wiz><div jscontroller="X" data-n-a-sg="SG123" data-n-a-ts="1700000000"></div></c-wiz></html>'
)

def batchexecute_body(decoded_url: str) -> str:
    # The decoder parses: text.split("\n\n")[1] -> json.loads(...)[:-2]
    # -> json.loads(parsed[0][2])[1]. Need >=3 outer elements so [:-2] leaves >=1.
    inner_json = json.dumps([None, decoded_url])  # json.loads(inner)[1] = url
    frame = ["wrb.fr", None, inner_json, None, None]  # frame[2] = inner_json
    outer = [frame, "pad1", "pad2"]  # 3 elems -> [:-2] leaves [frame]
    return ")]}'\n\n" + json.dumps(outer) + "\n"


def _fake_fetcher_factory(responses):
    """responses: list of (ok, status, body) returned in order; records tasks.

    Builds real FetchResult/Document objects (src.core.http / src.core.models)
    so the decoder runs against the genuine result shapes, not duck types.
    """
    from src.core.http import FetchResult
    from src.core.models import Document

    class FakeFetcher:
        def __init__(self):
            self.tasks = []

        def get(self, task, **kw):
            self.tasks.append(task)
            ok, status, body = responses.pop(0)
            doc = None
            if body is not None:
                doc = Document(
                    doc_id="d",
                    source="google_news",
                    url=task.url,
                    domain="news.google.com",
                    body=body,
                    status=status,
                )
            return FetchResult(ok=ok, status=status, doc=doc, cached=False, error=None, elapsed_ms=1)

    return FakeFetcher()


def test_extract_token_variants():
    assert extract_token("https://news.google.com/rss/articles/CBMiABC?oc=5") == "CBMiABC"
    assert extract_token("https://news.google.com/articles/CBMiABC") == "CBMiABC"
    assert extract_token("https://news.google.com/read/CBMiABC") == "CBMiABC"
    assert extract_token("https://example.com/x") is None
    assert extract_token("https://news.google.com/rss/headlines/section/topic/BUSINESS") is None


def test_fetch_decoding_params_parses_signature_and_timestamp():
    f = _fake_fetcher_factory([(True, 200, PARAMS_HTML)])
    params = fetch_decoding_params(f, "CBMiABC")
    assert params == ("SG123", "1700000000")
    t = f.tasks[0]
    assert t.method == "GET"
    assert t.url == "https://news.google.com/rss/articles/CBMiABC"
    assert t.source == "google_news"


def test_fetch_decoding_params_missing_attrs_is_none():
    f = _fake_fetcher_factory([(True, 200, b"<html><c-wiz><div jscontroller='X'></div></c-wiz></html>")])
    assert fetch_decoding_params(f, "CBMiABC") is None


def test_post_decode_sends_form_payload_and_parses_url():
    f = _fake_fetcher_factory([(True, 200, batchexecute_body("https://arlnow.com/story").encode())])
    url = post_decode(f, "SG123", "1700000000", "CBMiABC")
    assert url == "https://arlnow.com/story"
    t = f.tasks[0]
    assert t.method == "POST"
    assert t.url == "https://news.google.com/_/DotsSplashUi/data/batchexecute"
    assert t.data_body.startswith("f.req=")
    assert "CBMiABC" in t.data_body and "SG123" in t.data_body


def test_post_decode_garbage_response_is_none():
    f = _fake_fetcher_factory([(True, 200, b"not-a-batchexecute-response")])
    assert post_decode(f, "SG", "1", "T") is None


def test_decode_token_full_flow_success():
    f = _fake_fetcher_factory(
        [
            (True, 200, PARAMS_HTML),
            (True, 200, batchexecute_body("https://arlnow.com/story").encode()),
        ]
    )
    assert decode_token(f, "CBMiABC") == "https://arlnow.com/story"
    assert [t.method for t in f.tasks] == ["GET", "POST"]


def test_decode_token_params_failure_short_circuits():
    f = _fake_fetcher_factory([(False, 503, b"<html>err</html>")])
    assert decode_token(f, "CBMiABC") is None
    assert len(f.tasks) == 1  # params page failed -> no batchexecute POST


# ---------------------------------------------------------------------------
# DomainResolver: offline tiers -> sqlite cache -> bounded fetcher decodes
# ---------------------------------------------------------------------------


def test_domain_resolver_offline_first(tmp_path, monkeypatch):
    """?url= and summary tiers resolve with ZERO fetches/decodes."""
    from src.core.db import Database
    from src.sources.news.resolve import DomainResolver

    db = Database(tmp_path / "x.db")
    calls = []
    monkeypatch.setattr(
        "src.sources.news.decode.decode_token",
        lambda fetcher, token: calls.append(token) or "https://x.test/a",
    )
    r = DomainResolver(fetcher=object(), db=db, max_decodes=2, now="2026-10-02T00:00:00+00:00")
    assert r.resolve("https://news.google.com/rss/articles/T1?url=https%3A%2F%2Farlnow.com%2Fs", None) == "arlnow.com"
    assert calls == []  # offline tier — decoder never touched


def test_domain_resolver_decodes_misses_and_caches(tmp_path, monkeypatch):
    from src.core.db import Database
    from src.sources.news.resolve import DomainResolver

    db = Database(tmp_path / "x.db")
    calls = []
    monkeypatch.setattr(
        "src.sources.news.decode.decode_token",
        lambda fetcher, token: calls.append(token) or "https://www.arlnow.com/story",
    )
    r = DomainResolver(fetcher=object(), db=db, max_decodes=2, now="2026-10-02T00:00:00+00:00")
    assert r.resolve("https://news.google.com/rss/articles/T2", None) == "arlnow.com"
    assert calls == ["T2"]
    # Second resolver instance (next cycle, fresh budget) hits the cache, not the wire
    r2 = DomainResolver(fetcher=object(), db=db, max_decodes=2, now="2026-10-02T01:00:00+00:00")
    assert r2.resolve("https://news.google.com/rss/articles/T2", None) == "arlnow.com"
    assert calls == ["T2"]


def test_domain_resolver_budget_caps_decodes(tmp_path, monkeypatch):
    from src.core.db import Database
    from src.sources.news.resolve import DomainResolver

    db = Database(tmp_path / "x.db")
    calls = []
    monkeypatch.setattr(
        "src.sources.news.decode.decode_token",
        lambda fetcher, token: calls.append(token) or None,  # failures
    )
    r = DomainResolver(fetcher=object(), db=db, max_decodes=2, now="2026-10-02T00:00:00+00:00")
    for tok in ("A", "B", "C", "D"):
        assert r.resolve(f"https://news.google.com/rss/articles/{tok}", None) is None
    assert calls == ["A", "B"]  # budget exhausted; failures NOT cached (retry next cycle)


def test_domain_resolver_failure_memoized_one_attempt_per_cycle(tmp_path, monkeypatch):
    """A dead token costs ONE decode attempt per resolver (cycle): the failure
    is memoized, so repeats of the same link in one feed skip the wire
    entirely. The cache stays success-only, so the NEXT cycle still retries."""
    from src.core.db import Database
    from src.sources.news.resolve import DomainResolver

    db = Database(tmp_path / "x.db")
    calls = []

    def flaky(fetcher, token):
        calls.append(token)
        return "https://arlnow.com/s" if len(calls) > 1 else None  # fails only attempt 1

    monkeypatch.setattr("src.sources.news.decode.decode_token", flaky)
    r = DomainResolver(fetcher=object(), db=db, max_decodes=5, now="2026-10-02T00:00:00+00:00")
    assert r.resolve("https://news.google.com/rss/articles/T3", None) is None
    # same link again on the SAME resolver: memoized, NOT the success
    assert r.resolve("https://news.google.com/rss/articles/T3", None) is None
    assert calls == ["T3"]  # exactly ONE decode attempt per token per cycle
    # next cycle (fresh instance): the failure wasn't cached -> it retries,
    # and the fake (flaky only on attempt 1) now succeeds
    r2 = DomainResolver(fetcher=object(), db=db, max_decodes=5, now="2026-10-02T01:00:00+00:00")
    assert r2.resolve("https://news.google.com/rss/articles/T3", None) == "arlnow.com"
    assert calls == ["T3", "T3"]
