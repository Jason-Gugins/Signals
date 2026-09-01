"""Task 25: generic five-state selfcheck runner (src/core/selfcheck.py).

States: ok / drift / empty / challenge / error.
drift = source-specific markup present but the extractor parsed 0 items.
Each representative source (techstack, news_rss, ats_greenhouse) exercises all
five states through the REAL extractors used by the CLI `selfcheck` command,
with a mocked fetcher (no network).
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.core.http import FetchResult
from src.core.models import Document


def _fetch_result(body: bytes, *, ok: bool = True, status: int = 200) -> FetchResult:
    return FetchResult(
        ok=ok, status=status,
        doc=Document(doc_id="d", source="selfcheck", url="https://x/", body=body),
        cached=False, error=None, elapsed_ms=10,
    )


def _fetcher(body: bytes | None, *, ok: bool = True, status: int = 200):
    f = MagicMock()
    if body is None:
        f.fetch.return_value = FetchResult(ok=False, status=status, doc=None,
                                           cached=False, error="boom", elapsed_ms=10)
    else:
        f.fetch.return_value = _fetch_result(body, ok=ok, status=status)
    return f


# ------------------------------------------------------------- profiles -----
# Mirrors the CLI mapping (src/cli.py _selfcheck_source) with REAL extractors.

def _techstack_profile():
    from src.sources.techstack.fingerprint import (
        classify_cloudflare_challenge,
        extract_http_evidence,
        load_fingerprint_rules,
        match_fingerprints,
    )
    rules = load_fingerprint_rules()
    url = "https://acme.com/"

    def extract(html: str):
        ev = extract_http_evidence(html.encode("utf-8", "replace"), {}, url)
        return match_fingerprints(ev, rules)

    return dict(url=url, source="techstack", domain="acme.com", extract=extract,
                markup_hints=[b"__NEXT_DATA__", b"technologies"],
                detect_challenge=lambda status, body:
                    classify_cloudflare_challenge(status=status, body=body) is not None)


def _news_rss_profile():
    from src.sources.news.feeds import parse_feed
    url = "https://acme.com/feed.xml"

    def extract(html: str):
        return parse_feed(html.encode("utf-8", "replace"))

    return dict(url=url, source="news_rss", domain="acme.com", extract=extract,
                markup_hints=[b"<item", b"<entry"],
                detect_challenge=lambda status, body: b"Just a moment" in body)


def _ats_greenhouse_profile():
    from src.sources.ats.greenhouse import parse_greenhouse
    url = "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"

    def extract(html: str):
        return parse_greenhouse(html.encode("utf-8", "replace"))

    return dict(url=url, source="ats_greenhouse", domain="boards-api.greenhouse.io",
                extract=extract, markup_hints=[b"jobs"],
                detect_challenge=lambda status, body: b'"maintenance"' in body)


PROFILES = {
    "techstack": _techstack_profile,
    "news_rss": _news_rss_profile,
    "ats_greenhouse": _ats_greenhouse_profile,
}

BODIES = {
    "techstack": dict(
        ok=b'<html><script src="https://js.hs-scripts.com/1.js"></script>'
           b'<script>__NEXT_DATA__</script><p>technologies</p></html>',
        drift=b"<html><p>__NEXT_DATA__ technologies</p></html>",
        empty=b"<html><p>hello</p></html>",
        challenge=b"<html><title>Just a moment...</title></html>",
    ),
    "news_rss": dict(
        ok=b"<?xml version='1.0'?><rss><channel>"
           b"<item><title>News A</title><link>https://n.example/a</link></item>"
           b"</channel></rss>",
        # <item> markup present, but the entry has no title/link -> 0 parsed.
        drift=b"<rss><channel><item><nope/></item></channel></rss>",
        empty=b"<html><p>no feed here</p></html>",
        challenge=b"<html><title>Just a moment...</title></html>",
    ),
    "ats_greenhouse": dict(
        ok=json.dumps({"jobs": [{"id": 1, "title": "Engineer",
                                 "absolute_url": "https://j/1",
                                 "updated_at": "2026-01-01T00:00:00Z",
                                 "location": {"name": "New York, NY"}}]}).encode(),
        # 'jobs' key present but the list parses to 0 posts.
        drift=b'{"jobs": [], "paging": {"total": 0}}',
        empty=b'{"meta": {"total": 0}}',
        challenge=b'{"maintenance": true}',
    ),
}


@pytest.fixture(params=["techstack", "news_rss", "ats_greenhouse"])
def profile(request):
    return request.param, PROFILES[request.param]()


def test_ok_state(profile):
    name, p = profile
    from src.core.selfcheck import run_source_selfcheck
    r = run_source_selfcheck(_fetcher(BODIES[name]["ok"]), **p)
    assert r.state == "ok" and r.review_count >= 1 and r.url == p["url"]


def test_drift_state_is_markup_present_but_zero_parsed(profile):
    """drift == extractor returns 0 while the source-specific markup IS present."""
    name, p = profile
    from src.core.selfcheck import run_source_selfcheck
    r = run_source_selfcheck(_fetcher(BODIES[name]["drift"]), **p)
    assert r.state == "drift" and r.review_count == 0


def test_empty_state(profile):
    name, p = profile
    from src.core.selfcheck import run_source_selfcheck
    r = run_source_selfcheck(_fetcher(BODIES[name]["empty"]), **p)
    assert r.state == "empty" and r.review_count == 0


def test_challenge_state(profile):
    name, p = profile
    from src.core.selfcheck import run_source_selfcheck
    r = run_source_selfcheck(_fetcher(BODIES[name]["challenge"]), **p)
    assert r.state == "challenge" and r.review_count == 0


def test_error_state_when_fetch_raises(profile):
    name, p = profile
    from src.core.selfcheck import run_source_selfcheck
    f = MagicMock()
    f.fetch.side_effect = RuntimeError("connection reset")
    r = run_source_selfcheck(f, **p)
    assert r.state == "error" and "connection reset" in r.detail


def test_error_state_when_fetch_returns_no_document(profile):
    name, p = profile
    from src.core.selfcheck import run_source_selfcheck
    r = run_source_selfcheck(_fetcher(None, ok=False, status=503), **p)
    assert r.state == "error"


def test_selfcheckresult_reexport_from_marketplace():
    """Existing imports keep working: SelfcheckResult comes from src.core now."""
    from src.core.selfcheck import SelfcheckResult as CoreResult
    from src.sources.marketplace.selfcheck import SelfcheckResult as MktResult
    assert MktResult is CoreResult


# ------------------------------------------------------------------- CLI ----

def test_cli_source_mapping_builds_sample_urls_from_account_fields():
    from src.cli import _selfcheck_source
    u, src, _dom, _extract, hints, detect = _selfcheck_source(
        "ats_greenhouse", domain="acme.com", token="acme-board")
    assert u == "https://boards-api.greenhouse.io/v1/boards/acme-board/jobs?content=true"
    assert src == "ats_greenhouse" and hints == [b"jobs"]
    assert detect(200, b'{"maintenance": true}')

    u, src, _dom, _extract, hints, detect = _selfcheck_source(
        "techstack", domain="acme.com")
    assert u == "https://acme.com/" and hints == [b"technologies", b"__NEXT_DATA__"]
    assert detect(200, b"Just a moment") and not detect(200, b"<html></html>")

    u, src, _dom, _extract, hints, detect = _selfcheck_source(
        "news_rss", url="https://acme.com/blog/feed.xml")
    assert u == "https://acme.com/blog/feed.xml" and hints == [b"<item", b"<entry"]
    assert detect(200, b"Just a moment")


def test_cli_selfcheck_challenge_and_empty_do_not_crash(monkeypatch, tmp_path):
    """A challenge (or empty) selfcheck must exit cleanly, not traceback."""
    import click.testing

    import src.cli as cli

    bodies = {"ats_greenhouse": b'{"maintenance": true}',
              "techstack": b"<html><title>Just a moment...</title></html>",
              "news_rss": b"<html><title>Just a moment...</title></html>"}

    class FakeFetcher:
        def __init__(self, cfg, raw):
            pass

        def fetch(self, url, **kwargs):
            return _fetch_result(bodies.get(kwargs.get("source"), b"<html></html>"))

    monkeypatch.setattr("src.core.patchright_browser.PatchrightBrowserFetcher",
                        FakeFetcher)
    fake_cfg = SimpleNamespace(
        browser=SimpleNamespace(headless=False, user_agent="UA"),
        storage=SimpleNamespace(db_path=str(tmp_path / "db.sqlite3"),
                                raw_dir=str(tmp_path / "raw")),
    )
    monkeypatch.setattr(cli, "Config", SimpleNamespace(load=lambda path=None: fake_cfg))
    runner = click.testing.CliRunner()
    for source, opts in (("ats_greenhouse", ["--token", "acme"]),
                         ("techstack", ["--domain", "acme.com"]),
                         ("news_rss", ["--url", "https://acme.com/feed.xml"])):
        result = runner.invoke(cli.main, ["selfcheck", "--source", source] + opts)
        assert result.exit_code == 0, f"{source}: {result.output!r} {result.exception!r}"
        assert "challenge" in result.output


def test_cli_selfcheck_drift_fails_loudly(monkeypatch, tmp_path):
    import click.testing

    import src.cli as cli

    class FakeFetcher:
        def __init__(self, cfg, raw):
            pass

        def fetch(self, url, **kwargs):
            return _fetch_result(b'{"jobs": [], "paging": {"total": 0}}')

    monkeypatch.setattr("src.core.patchright_browser.PatchrightBrowserFetcher",
                        FakeFetcher)
    fake_cfg = SimpleNamespace(
        browser=SimpleNamespace(headless=False, user_agent="UA"),
        storage=SimpleNamespace(db_path=str(tmp_path / "db.sqlite3"),
                                raw_dir=str(tmp_path / "raw")),
    )
    monkeypatch.setattr(cli, "Config", SimpleNamespace(load=lambda path=None: fake_cfg))
    runner = click.testing.CliRunner()
    result = runner.invoke(cli.main, ["selfcheck", "--source", "ats_greenhouse",
                                      "--token", "acme"])
    assert result.exit_code == 1
    assert "drift" in result.output
