# tests/test_xray_cli.py
"""CLI tests for `src.cli xray` — offline via the _xray_fetch seam.

Established click-group test pattern (tests/test_cli_pipeline.py): monkeypatch
at class level, never seed ctx.obj. Two seams make the command offline:

- ``cli._xray_fetch`` — the module-level fetch-adapter factory; tests
  monkeypatch IT, never the transport.
- ``cli.XRAY_LEDGER_PATH`` — module global read at call time, so each test
  points the command at its own tmp_path ledger.

Runner contract pinned from the landed code (tests/test_xray_runner_offline.py):
an empty-after-filter library is unreachable with the shipped library (every
kind ships >=1 string), and the runner itself ledger-and-skips empty queries —
so the no-match/zero-shape contracts are pinned via --limit 0 and the loader
seam, per the dispatch's "code wins over the sketch" rule.
"""
import json
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

import src.cli as cli
import src.core.curl_fetcher as curl_fetcher_mod
from src.pipeline.orchestrator import Orchestrator


# Small synthetic organic ddg-lite body, same markup shape as the SYNTHETIC in
# tests/test_xray_serp_parse.py — ONE company URL (newco.io), uddg-wrapped.
ORG_COMPANY = (
    '<html><body>\n<table border="0">\n'
    '  <tr><td>1.&nbsp;</td>'
    '<td><a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fnewco.io%2F&amp;rut=cafe" '
    "class='result-link'>Newco is hiring</a></td></tr>\n"
    '  <tr><td></td><td class="result-snippet">Newco is hiring a head of sales.</td></tr>\n'
    "</table></body></html>"
)


class FakeDb:
    """Offline Database stand-in: canned cohort read, recorded upserts."""

    def __init__(self):
        self.upserts = []  # (table, row)

    def query(self, sql, *a, **k):
        return [{"domain": "knownco.com", "name": "KnownCo"}]

    def upsert(self, table, row, **k):
        self.upserts.append((table, row))
        return row

    def execute(self, sql, *a, **k):
        return []


def _boom_fetch(url):
    raise AssertionError(f"fetch must not be called in this test (got {url!r})")


@pytest.fixture
def fake_orch(tmp_path, monkeypatch):
    """Patched Orchestrator (no real db), patched ledger path, no real sleeps."""
    db = FakeDb()
    monkeypatch.setattr(Orchestrator, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(Orchestrator, "db", db, raising=False)
    monkeypatch.setattr(cli, "XRAY_LEDGER_PATH", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    return db


# --- 1. company run end-to-end offline: report + identity_candidates + ledger

def test_xray_company_run_offline(fake_orch, monkeypatch):
    monkeypatch.setattr(cli, "_xray_fetch", lambda: (lambda url: (200, ORG_COMPANY)))
    runner = CliRunner()
    result = runner.invoke(
        cli.main,
        [
            "xray",
            "--kind", "hiring",
            "--role", "head of sales",
            "--limit", "1",
            "--pace", "0",
            "--attempt-pause", "0",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "identity_candidates rows: 1" in result.output

    candidate_rows = [
        row for table, row in fake_orch.upserts if table == "identity_candidates"
    ]
    assert len(candidate_rows) == 1
    candidates = json.loads(candidate_rows[0]["candidates_json"])
    assert any(c["domain"] == "newco.io" for c in candidates)

    ledger = Path(cli.XRAY_LEDGER_PATH)
    assert ledger.exists()
    events = [
        json.loads(line)
        for line in ledger.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(events) == 1
    assert events[0]["status"] == "ok"


# --- 2. --stats prints the per-string table from the ledger -----------------

def test_xray_stats_prints_table(fake_orch):
    ledger = Path(cli.XRAY_LEDGER_PATH)
    events = [
        {"string_id": "s1", "query": "q1", "status": "ok", "results": 3,
         "profile_hits": 0, "company_hits": 1, "ts_utc": "2026-10-01T00:00:00+00:00"},
        {"string_id": "s1", "query": "q2", "status": "ok", "results": 1,
         "profile_hits": 2, "company_hits": 0, "ts_utc": "2026-10-02T00:00:00+00:00"},
    ]
    ledger.write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events),
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(cli.main, ["xray", "--stats"])
    assert result.exit_code == 0, result.output
    row = next(line for line in result.output.splitlines() if line.startswith("s1"))
    assert "\t2\t" in row  # runs == 2 on the s1 line
    assert "2026-10-02T00:00:00+00:00" in row  # last_ts_utc is the max stamp


# --- 3. unknown kind is a click usage error, never a fetch ------------------

def test_xray_unknown_kind_fails_cleanly(fake_orch, monkeypatch):
    monkeypatch.setattr(cli, "_xray_fetch", lambda: _boom_fetch)
    runner = CliRunner()
    result = runner.invoke(cli.main, ["xray", "--kind", "bogus"])
    assert result.exit_code != 0
    # click 8.1 usage-error wording (8.2+ says "invalid choice")
    assert "is not one of" in result.output


# --- 4. zero-shape contracts: --limit 0 caps before any fetch; an empty
#        after-filter library exits 1 with the no-match message ---------------

def test_xray_zero_limit_and_empty_library_guard(fake_orch, monkeypatch):
    monkeypatch.setattr(cli, "_xray_fetch", lambda: _boom_fetch)
    runner = CliRunner()

    # Real runner contract: max_queries is checked before starting a query,
    # so --limit 0 fetches nothing and still reports a visible zero run.
    result = runner.invoke(cli.main, ["xray", "--kind", "hiring", "--limit", "0"])
    assert result.exit_code == 0, result.output
    assert "0 queries" in result.output

    # The no-match guard is unreachable via the shipped library (every kind
    # ships >=1 string), so drive it through the loader seam.
    monkeypatch.setattr(cli, "load_library", lambda *a, **k: [])
    result = runner.invoke(cli.main, ["xray", "--kind", "intent"])
    assert result.exit_code == 1
    assert "no strings match" in result.output


# --- 5. google_state engine (bypass ladder Task 6) ---------------------------
#
# The google_state fetch factory loads the browser-harvested cookie jar
# (cli.XRAY_GOOGLE_COOKIES_PATH, same read-at-call-time seam as the ledger
# path); the transport boundary is faked at src.core.curl_fetcher
# .curl_cffi_get (the module's own documented test seam) so the real factory +
# real CurlCffiFetcher cookie plumbing runs offline.

GS_BODY = (
    "<html><head><title>q - Google Search</title></head><body>"
    '<script>(function(){var m={"k1":{"2003":[null,"tok1",'
    '"https://newco.io/","Newco is hiring",null,0]}};var a=m;'
    "if(window.W_jd)for(var b in a)window.W_jd[b]=a[b];"
    "else window.W_jd=a;})();</script></body></html>"
)


def _fake_curl_get(recorder: dict):
    from types import SimpleNamespace

    def fake_get(url, **kwargs):
        recorder["url"] = url
        recorder["cookie_header"] = (kwargs.get("headers") or {}).get("Cookie")
        # curl_cffi raw-response shape: status_code + .content bytes
        return SimpleNamespace(
            status_code=200, content=GS_BODY.encode("utf-8"), cookies={}, headers={}, url=url
        )

    return fake_get


def _write_jar(path: Path) -> None:
    path.write_text(
        json.dumps([{"name": "NID", "value": "v1", "domain": ".google.com"}]),
        encoding="utf-8",
    )


def test_xray_google_state_missing_cookie_jar_fails_cleanly(fake_orch, tmp_path, monkeypatch):
    # No jar -> plain-language error pointing at the refresh procedure, exit 1.
    monkeypatch.setattr(cli, "XRAY_GOOGLE_COOKIES_PATH", str(tmp_path / "nope.json"))
    runner = CliRunner()
    result = runner.invoke(
        cli.main,
        ["xray", "--engine", "google_state", "--kind", "hiring",
         "--role", "head of sales", "--limit", "1"],
    )
    assert result.exit_code != 0
    assert "no google cookie jar at" in result.output
    assert "scripts/google_cookie_refresh.py" in result.output


def test_xray_google_state_run_offline(fake_orch, tmp_path, monkeypatch):
    jar = tmp_path / "google_cookies.json"
    _write_jar(jar)
    monkeypatch.setattr(cli, "XRAY_GOOGLE_COOKIES_PATH", str(jar))
    seen: dict = {}
    monkeypatch.setattr(curl_fetcher_mod, "curl_cffi_get", _fake_curl_get(seen))
    runner = CliRunner()
    result = runner.invoke(
        cli.main,
        ["xray", "--engine", "google_state", "--kind", "hiring",
         "--role", "head of sales", "--limit", "1", "--pace", "0", "--attempt-pause", "0"],
    )
    assert result.exit_code == 0, result.output
    assert "engine: google_state" in result.output
    assert "identity_candidates rows: 1" in result.output
    assert seen["url"].startswith("https://www.google.com/search?q=")
    assert seen["cookie_header"] == "NID=v1"  # jar -> Cookie header

    events = [
        json.loads(line)
        for line in Path(cli.XRAY_LEDGER_PATH).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(events) == 1
    assert events[0]["engine"] == "google_state"
    assert events[0]["status"] == "ok"


def test_xray_default_engine_from_config(fake_orch, tmp_path, monkeypatch):
    # xray.default_engine: "ddg_lite" resolves without --engine; "google_state"
    # from config flows exactly like the explicit flag.
    jar = tmp_path / "google_cookies.json"
    _write_jar(jar)
    monkeypatch.setattr(cli, "XRAY_GOOGLE_COOKIES_PATH", str(jar))
    seen: dict = {}
    monkeypatch.setattr(curl_fetcher_mod, "curl_cffi_get", _fake_curl_get(seen))
    monkeypatch.setattr(
        cli.Config,
        "load_yaml",
        lambda self, name: {"xray": {"default_engine": "google_state"}}
        if name == "default"
        else {},
    )
    runner = CliRunner()
    result = runner.invoke(
        cli.main,
        ["xray", "--kind", "hiring", "--role", "head of sales",
         "--limit", "1", "--pace", "0", "--attempt-pause", "0"],
    )
    assert result.exit_code == 0, result.output
    assert "engine: google_state" in result.output  # config default, no --engine
    assert seen["url"].startswith("https://www.google.com/search?q=")


def test_xray_default_engine_falls_back_to_ddg_lite(fake_orch, monkeypatch):
    # No xray block in the config -> the documented fallback "ddg_lite".
    monkeypatch.setattr(cli.Config, "load_yaml", lambda self, name: {})
    monkeypatch.setattr(cli, "_xray_fetch", lambda: (lambda url: (200, ORG_COMPANY)))
    runner = CliRunner()
    result = runner.invoke(
        cli.main,
        ["xray", "--kind", "hiring", "--role", "head of sales",
         "--limit", "1", "--pace", "0", "--attempt-pause", "0"],
    )
    assert result.exit_code == 0, result.output
    assert "engine: ddg_lite" in result.output


def test_xray_bogus_config_engine_fails_cleanly(fake_orch, monkeypatch):
    # A bad value in config (bypasses click.Choice) is a clean usage error,
    # never a traceback and never a fetch.
    monkeypatch.setattr(
        cli.Config,
        "load_yaml",
        lambda self, name: {"xray": {"default_engine": "bing"}}
        if name == "default"
        else {},
    )
    runner = CliRunner()
    result = runner.invoke(cli.main, ["xray", "--kind", "hiring", "--limit", "1"])
    assert result.exit_code != 0
    assert "unknown xray engine" in result.output
