"""Task 14 Feature 1 — response-header evidence -> fingerprint matching.

The HTTP fetcher throws response headers away: ``extract_http_evidence``
accepts a headers dict but every caller passes ``{}`` because
``FetchResult``/``Document`` never carried them. This wave plumbs the chain
end to end:

- ``FetchResult.headers`` captures the 2xx response headers (lowercased keys,
  in-memory only — no DB change);
- the runner injects them into parse meta as ``response_headers`` (only when
  non-empty);
- ``match_fingerprints`` grows a ``header`` match type: a rule like
  ``match: {header: {x-powered-by: "next.js"}}`` hits when ANY (key, value)
  pair is present in the response headers with a casefolded substring value
  match;
- techstack parse/harvest pass ``meta["response_headers"]`` through
  ``extract_http_evidence`` so named vendors can match on headers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Optional

import httpx
import respx

from src.core.config import Config
from src.core.db import Database
from src.core.http import HttpFetcher
from src.core.models import Account, Document
from src.core.ratelimit import RateLimiter
from src.core.rawstore import RawStore
from src.core.runlog import RunContext
from src.pipeline.runner import CollectorRunner
from src.signals.store import SignalStore
from src.signals.taxonomy import Taxonomy
from src.identity.registry import AccountRegistry
from src.sources.base import FetchTask, SignalCandidate, SourceAdapter
from src.sources.techstack.collector import TechstackSource
from src.sources.techstack.fingerprint import extract_http_evidence, match_fingerprints

TODAY = "2026-09-05"


# ── harness (mirrors tests/test_http.py and tests/test_runner_stability.py) ──


@dataclass
class Task:
    source: str
    url: str
    domain: Optional[str] = None
    method: str = "GET"
    headers: dict = field(default_factory=dict)
    json_body: Optional[dict] = None


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self):
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _fetcher(tmp_path, monkeypatch, client=None):
    monkeypatch.setenv("SIGNALS_CONTACT_EMAIL", "ops@example.com")
    env = tmp_path / "empty.env"
    env.write_text("", encoding="utf-8")
    cfg = Config.load(env_path=env)
    db = Database(tmp_path / "signals.db")
    store = RawStore(db, raw_dir=tmp_path / "raw")
    clock = FakeClock()
    limiter = RateLimiter(default_rate=100.0, clock=clock, sleep=clock.sleep)
    ctx = RunContext(db, stage="collect")
    ctx.__enter__()
    fetcher = HttpFetcher(
        cfg, store, limiter, ctx=ctx, client=client, sleep=clock.sleep, rng=lambda a, b: 0.0
    )
    return fetcher, store, ctx, db, clock


# ── FetchResult carries response headers ─────────────────────────────────────


@respx.mock
def test_200_captures_response_headers_lowercased(tmp_path, monkeypatch):
    url = "https://example.com/"
    respx.get(url).mock(
        return_value=httpx.Response(
            200,
            content=b"<html></html>",
            headers={"X-Powered-By": "Next.js", "Server": "cloudflare"},
        )
    )
    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
    fetcher, _store, ctx, _db, _clock = _fetcher(tmp_path, monkeypatch)
    result = fetcher.get(Task(source="techstack", url=url, domain="example.com"))
    ctx.__exit__(None, None, None)

    assert result.ok is True
    # Keys lowercased (extract_http_evidence contract), values untouched.
    # httpx adds transport headers (content-length), so assert on the
    # application headers rather than exact equality.
    assert result.headers["x-powered-by"] == "Next.js"
    assert result.headers["server"] == "cloudflare"
    assert all(k == k.lower() for k in result.headers)


@respx.mock
def test_non_2xx_result_has_empty_headers(tmp_path, monkeypatch):
    url = "https://example.com/missing"
    respx.get(url).mock(
        return_value=httpx.Response(404, headers={"Server": "cloudflare"})
    )
    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
    fetcher, _store, ctx, _db, _clock = _fetcher(tmp_path, monkeypatch)
    result = fetcher.get(Task(source="techstack", url=url, domain="example.com"))
    ctx.__exit__(None, None, None)

    assert result.ok is False
    assert result.headers == {}


def test_extract_http_evidence_carries_headers_into_evidence():
    ev = extract_http_evidence(b"<html></html>", {"X-Powered-By": "next.js"}, "https://acme.com/")
    assert ev.headers == {"x-powered-by": "next.js"}


# ── header match type in match_fingerprints ──────────────────────────────────

_HEADER_RULES = {
    "vendors": {
        "nextjs": {
            "display": "Next.js",
            "category": ["framework"],
            "tier": "mid",
            "match": {"header": {"x-powered-by": "next.js"}},
        },
        "pairwise": {
            "display": "Pairwise",
            "category": ["test"],
            "tier": "mid",
            "match": {"header": {"server": "cloudflare", "x-generator": "webflow"}},
        },
    }
}


def test_header_rule_matches_synthetic_evidence():
    ev = SimpleNamespace(headers={"x-powered-by": "next.js"})
    hits = match_fingerprints(ev, _HEADER_RULES)
    assert len(hits) == 1
    assert hits[0].vendor == "nextjs"
    assert hits[0].evidence == "header"
    assert hits[0].confidence == 0.8


def test_header_rule_value_is_casefolded_substring():
    # Response value carries a version suffix and mixed case — still hits.
    ev = SimpleNamespace(headers={"x-powered-by": "Next.js; ver=14"})
    assert [h.vendor for h in match_fingerprints(ev, _HEADER_RULES)] == ["nextjs"]
    # Needle case is folded too.
    ev2 = SimpleNamespace(headers={"x-powered-by": "next.js"})
    rules = {
        "vendors": {
            "nextjs": {"display": "N", "match": {"header": {"x-powered-by": "NEXT.JS"}}}
        }
    }
    assert [h.vendor for h in match_fingerprints(ev2, rules)] == ["nextjs"]


def test_header_rule_any_pair_hits():
    ev = SimpleNamespace(headers={"server": "cloudflare"})
    assert [h.vendor for h in match_fingerprints(ev, _HEADER_RULES)] == ["pairwise"]
    miss = SimpleNamespace(headers={"server": "nginx"})
    assert match_fingerprints(miss, _HEADER_RULES) == []


def test_header_rule_needs_headers():
    # HttpEvidence with empty headers: no hit.
    ev = extract_http_evidence(b"<html></html>", {}, "https://acme.com/")
    assert match_fingerprints(ev, _HEADER_RULES) == []
    # Evidence object with no headers attribute at all (network/DNS shapes):
    # no hit, no crash.
    assert match_fingerprints(SimpleNamespace(), _HEADER_RULES) == []


# ── runner injects response_headers into parse meta ──────────────────────────


class MetaCapture(SourceAdapter):
    key = "metacap"
    tier = "http"
    cadence_hours = 24
    requires = ()

    def __init__(self):
        self.seen: list[dict] = []

    def plan(self, account, cursor):
        return [FetchTask(source=self.key, url=f"https://{account.domain}/", domain=account.domain)]

    def parse(self, doc, account, task_meta):
        self.seen.append(dict(task_meta or {}))
        return []


class HeadersFetch:
    """Fetcher returning a FetchResult that carries response headers."""

    def __init__(self, headers: dict):
        self._headers = headers

    def get(self, task, *, etag=None, last_modified=None):
        from src.core.http import FetchResult

        doc = Document(
            doc_id=f"d-{task.source}", source=task.source, url=task.url,
            domain=task.domain, body=b"ok", status=200,
        )
        return FetchResult(True, 200, doc, False, None, 1, headers=self._headers)


def _runner(tmp_path, fetcher):
    db = Database(tmp_path / "runner.db")
    cfg = Config()
    cfg.http.max_workers = 1
    cfg.http.respect_robots = False
    store = RawStore(db, tmp_path / "raw")
    tax = Taxonomy.load()
    ctx = RunContext(db, "collect")
    ctx.__enter__()
    runner = CollectorRunner(
        cfg, db, AccountRegistry(db), store, fetcher,
        SignalStore(db, tax), tax, ctx,
    )
    return runner, db, ctx


def test_runner_meta_carries_response_headers(tmp_path):
    adapter = MetaCapture()
    runner, _db, ctx = _runner(tmp_path, HeadersFetch({"x-powered-by": "next.js"}))
    runner.run([adapter], [Account(domain="acme.com")], force=True)
    ctx.__exit__(None, None, None)

    assert len(adapter.seen) == 1
    assert adapter.seen[0]["response_headers"] == {"x-powered-by": "next.js"}


def test_runner_meta_omits_response_headers_when_empty(tmp_path):
    adapter = MetaCapture()
    runner, _db, ctx = _runner(tmp_path, HeadersFetch({}))
    runner.run([adapter], [Account(domain="acme.com")], force=True)
    ctx.__exit__(None, None, None)

    assert adapter.seen and "response_headers" not in adapter.seen[0]


# ── techstack parse/harvest consult the injected headers ─────────────────────


def _html_doc():
    return Document(
        doc_id="d", source="techstack", url="https://acme.com/",
        body=b"<html><body>hi</body></html>",
    )


def test_techstack_parse_header_hit_emits_install_candidate():
    cands = TechstackSource().parse(
        _html_doc(),
        Account(domain="acme.com"),
        {"kind": "html", "today": TODAY, "response_headers": {"server": "cloudflare"}},
    )
    keys = [c.natural_key for c in cands]
    assert "tech_install_new:cloudflare:2026-09" in keys


def test_techstack_parse_without_headers_emits_nothing_for_plain_html():
    cands = TechstackSource().parse(
        _html_doc(),
        Account(domain="acme.com"),
        {"kind": "html", "today": TODAY},
    )
    assert cands == []


def test_techstack_harvest_header_hit_names_vendor():
    matches = TechstackSource().harvest_tech(
        _html_doc(),
        Account(domain="acme.com"),
        {"kind": "html", "today": TODAY, "response_headers": {"server": "cloudflare"}},
    )
    assert any(m.vendor == "cloudflare" and m.evidence == "header" for m in matches)
