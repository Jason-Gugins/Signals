"""Task 7 (roadmap 2026-09-04): crt.sh cycle-over-cycle subdomain delta.

parse_crtsh already returns the full sorted subdomain set every cycle, but
nothing persisted it — so no delta existed. This pins:

- the parse() diff: ``meta["prev_crtsh_names"]`` (injected by the runner
  from the most recent prior crt.sh doc) diffs against the current set;
  new names become ``new_subdomain`` candidates with PERMANENT natural
  keys (``subdomain:{domain}:{name}`` — a subdomain that disappears and
  re-appears is not new again, dedupe is per key forever), capped at 50
  per cycle (hint-labeled names first, then the sorted remainder);
- the conservative contract: no prev (first run) or an EMPTY prev (a
  prior doc that parsed to zero names) -> no signal, never guess;
- the runner's ``prev_crtsh_names`` injection (mirror of the proven
  prev_pricing_html / prev_homepage_html mechanism) end-to-end with the
  real CrtshSource: persisted on the second cycle, deduped on the third;
- ``new_subdomain`` survives normalize_batch (taxonomy pre-registered).
"""

from __future__ import annotations

import json
from datetime import date

from src.core.models import Account
from src.signals.normalize import make_signal_id, normalize_batch
from src.signals.taxonomy import Taxonomy
from src.sources.crtsh.collector import CrtshSource

ACCT = Account(domain="acme.com", name="Acme")
TODAY = "2026-09-05"


def _body(names: list[str]) -> bytes:
    return json.dumps([{"name_value": n} for n in names]).encode("utf-8")


def _doc(body: bytes):
    return type("D", (), {"body": body})()


def _new_subdomain_cands(src, names: list[str], meta_extra: dict) -> list:
    cands = src.parse(_doc(_body(names)), ACCT, {"today": TODAY, **meta_extra})
    return [c for c in cands if c.signal_type == "new_subdomain"]


# ── parse() diff: new names, permanent keys, candidate shape ────────────────


def test_parse_emits_new_subdomains_with_permanent_keys():
    src = CrtshSource()
    prev = ["acme.com", "status.acme.com", "www.acme.com"]
    curr = ["acme.com", "app.acme.com", "beta.acme.com", "status.acme.com", "www.acme.com"]
    sub = _new_subdomain_cands(src, curr, {"prev_crtsh_names": prev})
    assert [(c.natural_key, c.title) for c in sub] == [
        ("subdomain:acme.com:app.acme.com", "app.acme.com"),
        ("subdomain:acme.com:beta.acme.com", "beta.acme.com"),
    ]
    cand = sub[0]
    assert cand.observed_at == TODAY
    assert cand.confidence == 0.5
    assert cand.evidence_data == {"subdomain": "app.acme.com"}


def test_parse_disappeared_names_are_ignored():
    """The delta is strictly current-minus-previous; names only in prev
    (or absent from both sides) never emit."""
    src = CrtshSource()
    prev = ["acme.com", "ghost.acme.com", "www.acme.com"]
    curr = ["acme.com", "www.acme.com"]
    assert _new_subdomain_cands(src, curr, {"prev_crtsh_names": prev}) == []


def test_parse_no_prev_and_empty_prev_emit_nothing():
    """First-run conservative contract: no injected prev (no prior doc) ->
    never guess. An EMPTY prev (a prior doc that parsed to zero names) must
    NOT make every current name 'new' either."""
    src = CrtshSource()
    curr = ["acme.com", "app.acme.com", "www.acme.com"]
    assert _new_subdomain_cands(src, curr, {}) == []
    assert _new_subdomain_cands(src, curr, {"prev_crtsh_names": []}) == []


def test_parse_hint_labeled_names_order_first_then_sorted_remainder():
    """Stable order under the cap: hint-labeled (app/status/jobs/...) first,
    then the sorted remainder. Sorted new names are api < help < jobs, but
    help/jobs carry hints so they come first."""
    src = CrtshSource()
    prev = ["acme.com", "www.acme.com"]
    curr = ["acme.com", "api.acme.com", "help.acme.com", "jobs.acme.com", "www.acme.com"]
    sub = _new_subdomain_cands(src, curr, {"prev_crtsh_names": prev})
    assert [c.title for c in sub] == ["help.acme.com", "jobs.acme.com", "api.acme.com"]


def test_parse_caps_new_subdomains_at_50_per_cycle():
    """A subdomain flood (wildcard cert leaks etc.) is bounded at 50
    candidates, hint-labeled names kept ahead of the sorted remainder."""
    src = CrtshSource()
    prev = ["acme.com", "www.acme.com"]
    hosts = [f"host{i:03d}.acme.com" for i in range(60)]
    curr = sorted(["acme.com", "status.acme.com", "www.acme.com"] + hosts)
    sub = _new_subdomain_cands(src, curr, {"prev_crtsh_names": prev})
    assert len(sub) == 50
    # status.acme.com is hint-labeled -> first under the cap.
    assert sub[0].natural_key == "subdomain:acme.com:status.acme.com"


def test_parse_tech_install_new_emission_unchanged():
    """The pre-existing SUBDOMAIN_HINTS tech_install_new emission keeps
    working alongside the delta."""
    src = CrtshSource()
    curr = ["acme.com", "status.acme.com", "www.acme.com"]
    cands = src.parse(_doc(_body(curr)), ACCT, {"today": TODAY, "prev_crtsh_names": curr})
    assert any(c.signal_type == "tech_install_new" and c.natural_key == "crt:statuspage" for c in cands)


# ── persistence (taxonomy survival, the tech_churn house rule) ──────────────


def test_new_subdomain_persists_through_normalize_batch():
    src = CrtshSource()
    prev = ["acme.com", "www.acme.com"]
    sub = _new_subdomain_cands(
        src, ["acme.com", "beta.acme.com", "www.acme.com"], {"prev_crtsh_names": prev}
    )
    assert len(sub) == 1
    valid, rejected = normalize_batch(
        sub,
        account=ACCT,
        source="crtsh",
        taxonomy=Taxonomy.load(),
        now="2026-09-05T00:00:00+00:00",
    )
    assert len(valid) == 1
    assert rejected == []
    assert valid[0].signal_type == "new_subdomain"
    assert valid[0].source == "crtsh"


# ── runner prev_crtsh_names injection (end-to-end, real adapter) ────────────


class _CrtshFetch:
    """Hands out the queued crt.sh body per fetch and stores each doc in the
    rawstore (content-addressed doc_id = sha256(body)) like the real fetcher
    does, so the runner's prior-doc lookup has something to find."""

    def __init__(self, store, bodies: list[bytes]):
        self.store = store
        self.bodies = list(bodies)

    def get(self, task, *, etag=None, last_modified=None):
        from src.core.http import FetchResult

        body = self.bodies.pop(0) if len(self.bodies) > 1 else self.bodies[0]
        doc = self.store.put(
            source=task.source,
            url=task.url,
            body=body,
            content_type="application/json",
            status=200,
            domain=task.domain,
        )
        return FetchResult(True, 200, doc, False, None, 1)


def _runner(tmp_path, bodies: list[bytes]):
    from src.core.config import Config
    from src.core.db import Database
    from src.core.rawstore import RawStore
    from src.core.runlog import RunContext
    from src.identity.registry import AccountRegistry
    from src.pipeline.runner import CollectorRunner
    from src.signals.store import SignalStore
    from src.signals.taxonomy import Taxonomy

    db = Database(tmp_path / "s.db")
    cfg = Config()
    cfg.http.max_workers = 1
    cfg.http.respect_robots = False
    store = RawStore(db, tmp_path / "raw")
    tax = Taxonomy.load()
    ctx = RunContext(db, "collect")
    ctx.__enter__()
    runner = CollectorRunner(
        cfg, db, AccountRegistry(db), store, _CrtshFetch(store, bodies),
        SignalStore(db, tax), tax, ctx,
    )
    return runner, db, ctx


def test_runner_persists_new_subdomain_on_second_cycle_dedupes_on_third(tmp_path):
    base = ["acme.com", "status.acme.com", "www.acme.com"]
    grown = sorted(base + ["beta.acme.com"])
    runner, db, ctx = _runner(tmp_path, [_body(base), _body(grown), _body(grown)])
    accounts = [Account(domain="acme.com", name="Acme")]
    try:
        # max_passes=1: one fetch per run, so each queued body maps to exactly
        # one cycle (the default second pass would re-fetch and consume the
        # next queued body early).
        # Cycle 1: no prior crt.sh doc -> no injection -> conservative, no
        # new_subdomain. The pre-existing hint emission still fires once
        # (status.acme.com -> tech_install_new crt:statuspage).
        stats1 = runner.run([CrtshSource()], accounts, force=True, max_passes=1)
        assert db.query("SELECT * FROM signals WHERE signal_type='new_subdomain'") == []
        assert stats1.by_source["crtsh"]["signals_new"] == 1

        # Cycle 2: the prior doc (cycle 1) is re-parsed by the runner and
        # injected as prev_crtsh_names -> beta.acme.com is new -> persisted.
        stats2 = runner.run([CrtshSource()], accounts, force=True, max_passes=1)
        rows = db.query("SELECT * FROM signals WHERE signal_type='new_subdomain'")
        assert len(rows) == 1, "prev_crtsh_names injection or the parse diff is broken"
        row = rows[0]
        assert row["domain"] == "acme.com"
        assert row["source"] == "crtsh"
        assert row["title"] == "beta.acme.com"
        # The natural key is hashed into signal_id — pin the PERMANENT key
        # (no date component) through the same derivation normalize uses.
        assert row["signal_id"] == make_signal_id(
            "acme.com", "new_subdomain", "subdomain:acme.com:beta.acme.com"
        )
        assert json.loads(row["evidence_data"]) == {"subdomain": "beta.acme.com"}
        assert stats2.by_source["crtsh"]["signals_new"] == 1

        # Cycle 3: identical body -> the content-addressed doc_id collides with
        # cycle 2's doc, so the prior doc is cycle 1's and the diff emits beta
        # again — but the PERMANENT natural key dedupes in the signal store
        # (a re-appearing subdomain is not new).
        stats3 = runner.run([CrtshSource()], accounts, force=True, max_passes=1)
        rows = db.query("SELECT * FROM signals WHERE signal_type='new_subdomain'")
        assert len(rows) == 1
        assert stats3.signals_new == 0
    finally:
        ctx.__exit__(None, None, None)
