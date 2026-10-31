"""Plan T4 — discovery waterfall composer (src/identity/discover.py) and the
Orchestrator.discover persistence wiring into identity_candidates.

Composer tests monkeypatch the module-level stage runners (_run_wikidata /
_run_wikipedia / _run_gkg / _run_ddg) so cross-stage logic is tested offline;
one end-to-end test runs the real waterfall through the real Wikidata
resolver with a canned fetcher.
"""

from __future__ import annotations

import json
from types import SimpleNamespace


def _stage(status: str, domain=None, candidates=None, **extra) -> dict:
    out = {"status": status, "domain": domain, "candidates": candidates or []}
    out.update(extra)
    return out


# ── composer: first-hit early exit ──────────────────────────────────────────

def test_first_hit_wikidata_resolved_skips_remaining(monkeypatch):
    from src.identity import discover as dw

    calls = []

    def wikidata(name, fetcher):
        calls.append("wikidata")
        return _stage(
            "resolved", "acme.com",
            [{"domain": "acme.com", "url": "https://acme.com", "score": 4, "label": "Acme"}],
        )

    def wikipedia(name, fetcher):
        calls.append("wikipedia")
        return _stage("no_match")

    def boom_gkg(name, backend):
        raise AssertionError("gkg must be skipped after a first hit")

    monkeypatch.setattr(dw, "_run_wikidata", wikidata)
    monkeypatch.setattr(dw, "_run_wikipedia", wikipedia)
    monkeypatch.setattr(dw, "_run_gkg", boom_gkg)

    out = dw.discover_waterfall("Acme Corp", object())

    assert out["name"] == "Acme Corp"
    assert out["status"] == "resolved"
    assert out["domain"] == "acme.com"
    assert calls == ["wikidata"]  # early exit: nothing after the hit ran
    assert out["stages"]["wikipedia"] == {"status": "skipped"}
    assert out["stages"]["gkg"] == {"status": "skipped"}
    assert "ddg" not in out["stages"]  # ddg is opt-in: key absent without the flag
    assert "agreement" not in out
    assert out["candidates"] == [
        {"domain": "acme.com", "url": "https://acme.com", "score": 4, "label": "Acme", "stage": "wikidata"}
    ]


def test_wikipedia_resolves_when_wikidata_no_match(monkeypatch):
    from src.identity import discover as dw

    calls = []

    monkeypatch.setattr(dw, "_run_wikidata", lambda n, f: calls.append("wikidata") or _stage("no_match"))
    monkeypatch.setattr(
        dw, "_run_wikipedia",
        lambda n, f: calls.append("wikipedia") or _stage(
            "resolved", "insight.io",
            [{"domain": "insight.io", "url": "https://insight.io", "score": 3, "title": "Insight"}],
        ),
    )

    def boom_gkg(name, backend):
        raise AssertionError("gkg must be skipped after a first hit")

    monkeypatch.setattr(dw, "_run_gkg", boom_gkg)

    out = dw.discover_waterfall("Insight", object())

    assert out["status"] == "resolved" and out["domain"] == "insight.io"
    assert calls == ["wikidata", "wikipedia"]
    assert out["stages"]["gkg"] == {"status": "skipped"}
    assert [c["stage"] for c in out["candidates"]] == ["wikipedia"]


# ── composer: 2-source apex agreement ───────────────────────────────────────

def test_two_source_agreement_promotes_to_resolved(monkeypatch):
    from src.identity import discover as dw

    monkeypatch.setattr(
        dw, "_run_wikidata",
        lambda n, f: _stage("ambiguous", candidates=[
            {"domain": "acme.com", "score": 2, "qid": "Q1", "label": "Acme"},
        ]),
    )
    monkeypatch.setattr(dw, "_run_wikipedia", lambda n, f: _stage("no_match"))
    monkeypatch.setattr(
        dw, "_run_gkg",
        lambda n, b: _stage("ambiguous", candidates=[
            # subdomain form must normalize onto the same apex
            {"domain": "www.acme.com", "score": 1, "entity_id": "c-1", "name": "Acme",
             "url": "https://www.acme.com/about"},
        ]),
    )

    out = dw.discover_waterfall("Acme", object())

    assert out["status"] == "resolved"
    assert out["domain"] == "acme.com"
    assert out["agreement"] == ["wikidata", "gkg"]
    # Both stages' candidates survive in the merged list, stage-annotated.
    assert [c["stage"] for c in out["candidates"]] == ["wikidata", "gkg"]


def test_single_source_no_agreement_stays_ambiguous(monkeypatch):
    from src.identity import discover as dw

    monkeypatch.setattr(
        dw, "_run_wikidata",
        lambda n, f: _stage("ambiguous", candidates=[{"domain": "acme.com", "score": 2, "qid": "Q1"}]),
    )
    monkeypatch.setattr(dw, "_run_wikipedia", lambda n, f: _stage("no_match"))
    monkeypatch.setattr(
        dw, "_run_gkg",
        lambda n, b: _stage("ambiguous", candidates=[{"domain": "initech.com", "score": 1, "entity_id": "c-2"}]),
    )

    out = dw.discover_waterfall("Acme", object())

    assert out["status"] == "ambiguous"
    assert out["domain"] is None
    assert "agreement" not in out


def test_agreement_needs_two_distinct_stages(monkeypatch):
    from src.identity import discover as dw

    # Two candidates for the same domain from ONE stage only: no promotion.
    monkeypatch.setattr(
        dw, "_run_wikidata",
        lambda n, f: _stage("ambiguous", candidates=[
            {"domain": "acme.com", "score": 2, "qid": "Q1"},
            {"domain": "acme.com", "score": 1, "qid": "Q2"},
        ]),
    )
    monkeypatch.setattr(dw, "_run_wikipedia", lambda n, f: _stage("no_match"))
    monkeypatch.setattr(dw, "_run_gkg", lambda n, b: _stage("no_match"))

    out = dw.discover_waterfall("Acme", object())

    assert out["status"] == "ambiguous"
    assert "agreement" not in out


def test_apex_agreement_tie_single_winner_by_stage_priority(monkeypatch):
    """Plan T5 test 5 — apex-agreement TIE pin: two rival apexes, each backed
    by two DISTINCT stage votes (wikidata+ddg for acme.com, wikipedia+ddg
    for acme.org). Read of _apex_agreement (discover.py:143-151): the rank
    tuple is (-len(agreeing), min stage index in STAGE_PRIORITY), so a tie
    on vote count is broken by the EARLIEST agreeing stage — a single
    resolved winner, NOT a second ambiguity round. acme.com (wikidata,
    priority index 0) beats acme.org (wikipedia, index 1)."""
    from src.identity import discover as dw

    monkeypatch.setattr(
        dw, "_run_wikidata",
        lambda n, f: _stage("ambiguous", candidates=[{"domain": "acme.com", "score": 2, "qid": "Q1"}]),
    )
    monkeypatch.setattr(
        dw, "_run_wikipedia",
        lambda n, f: _stage("ambiguous", candidates=[{"domain": "acme.org", "score": 3, "title": "Acme"}]),
    )
    monkeypatch.setattr(dw, "_run_gkg", lambda n, b: _stage("no_match"))
    monkeypatch.setattr(
        dw, "_run_ddg",
        lambda n, f, clock, sleep: _stage("ambiguous", candidates=[
            {"domain": "acme.com", "url": "https://acme.com", "score": 1, "title": "Acme", "position": 1},
            {"domain": "acme.org", "url": "https://acme.org", "score": 1, "title": "Acme.org", "position": 2},
        ]),
    )

    out = dw.discover_waterfall("Acme", object(), ddg_enabled=True)

    # PIN: single winner — the apex whose earliest agreeing stage ranks
    # higher in STAGE_PRIORITY. No stage resolved individually, so nothing
    # was skipped either.
    assert out["status"] == "resolved"
    assert out["domain"] == "acme.com"
    assert out["agreement"] == ["wikidata", "ddg"]
    assert [out["stages"][s]["status"] for s in ("wikidata", "wikipedia", "gkg", "ddg")] == [
        "ambiguous", "ambiguous", "no_match", "ambiguous",
    ]
    # Merged display ranking (score desc, then stage priority) still keeps
    # every stage's candidates: dedup is per stage, nothing fabricated.
    assert [(c["domain"], c["score"], c["stage"]) for c in out["candidates"]] == [
        ("acme.org", 3, "wikipedia"),
        ("acme.com", 2, "wikidata"),
        ("acme.com", 1, "ddg"),
        ("acme.org", 1, "ddg"),
    ]


# ── composer: ddg opt-in gating ─────────────────────────────────────────────

def test_ddg_skipped_without_flag_and_runs_with_it(monkeypatch):
    from src.identity import discover as dw

    ddg_calls = []

    monkeypatch.setattr(dw, "_run_wikidata", lambda n, f: _stage("no_match"))
    monkeypatch.setattr(dw, "_run_wikipedia", lambda n, f: _stage("no_match"))
    monkeypatch.setattr(dw, "_run_gkg", lambda n, b: _stage("unconfigured"))

    def ddg(name, fetcher, clock, sleep):
        ddg_calls.append({"name": name, "fetcher": fetcher})
        return _stage("ambiguous", candidates=[
            {"domain": "acme.io", "url": "https://acme.io", "score": 2, "title": "Acme", "position": 1},
        ])

    monkeypatch.setattr(dw, "_run_ddg", ddg)

    sentinel = object()
    out = dw.discover_waterfall("Acme", object(), ddg_enabled=True, ddg_fetcher=sentinel)

    assert ddg_calls == [{"name": "Acme", "fetcher": sentinel}]
    assert "ddg" in out["stages"]
    assert out["stages"]["ddg"]["status"] == "ambiguous"
    # No stage resolved and ddg alone voted acme.io -> stays ambiguous.
    assert out["status"] == "ambiguous"

    out_off = dw.discover_waterfall("Acme", object())
    assert ddg_calls == [{"name": "Acme", "fetcher": sentinel}]  # not called again
    assert "ddg" not in out_off["stages"]
    assert out_off["status"] == "no_match"


def test_ddg_skipped_after_first_hit_even_when_enabled(monkeypatch):
    from src.identity import discover as dw

    monkeypatch.setattr(
        dw, "_run_wikidata",
        lambda n, f: _stage("resolved", "acme.com", [{"domain": "acme.com", "score": 4}]),
    )

    def boom_ddg(name, fetcher, clock, sleep):
        raise AssertionError("ddg must never run after a first hit")

    monkeypatch.setattr(dw, "_run_ddg", boom_ddg)

    out = dw.discover_waterfall("Acme", object(), ddg_enabled=True)

    assert out["status"] == "resolved" and out["domain"] == "acme.com"
    assert out["stages"]["wikipedia"] == {"status": "skipped"}
    assert out["stages"]["gkg"] == {"status": "skipped"}
    assert out["stages"]["ddg"] == {"status": "skipped"}


# ── composer: per-stage error isolation ─────────────────────────────────────

def test_stage_error_isolated_waterfall_continues(monkeypatch):
    from src.identity import discover as dw

    def wikidata_boom(name, fetcher):
        raise RuntimeError("wikidata down")

    monkeypatch.setattr(dw, "_run_wikidata", wikidata_boom)
    monkeypatch.setattr(
        dw, "_run_wikipedia",
        lambda n, f: _stage("ambiguous", candidates=[{"domain": "acme.com", "score": 2, "title": "Acme"}]),
    )
    monkeypatch.setattr(
        dw, "_run_gkg",
        lambda n, b: _stage("ambiguous", candidates=[{"domain": "acme.com", "score": 1, "entity_id": "c-1"}]),
    )

    out = dw.discover_waterfall("Acme", object())

    # The crashed stage is recorded and the waterfall carried on to agreement.
    assert out["stages"]["wikidata"]["status"] == "error"
    assert "wikidata down" in out["stages"]["wikidata"]["error"]
    assert out["status"] == "resolved"
    assert out["domain"] == "acme.com"
    assert out["agreement"] == ["wikipedia", "gkg"]


def test_all_stages_error_yields_no_match(monkeypatch):
    from src.identity import discover as dw

    def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(dw, "_run_wikidata", boom)
    monkeypatch.setattr(dw, "_run_wikipedia", boom)
    monkeypatch.setattr(dw, "_run_gkg", boom)
    monkeypatch.setattr(dw, "_run_ddg", boom)

    out = dw.discover_waterfall("Acme", object(), ddg_enabled=True)

    assert out["status"] == "no_match"
    assert out["domain"] is None
    assert out["candidates"] == []
    for stage in ("wikidata", "wikipedia", "gkg", "ddg"):
        assert out["stages"][stage]["status"] == "error"


# ── composer: merged / deduped / ranked candidates ──────────────────────────

def test_merged_candidates_deduped_ranked_and_stage_attributed(monkeypatch):
    from src.identity import discover as dw

    monkeypatch.setattr(
        dw, "_run_wikidata",
        lambda n, f: _stage("ambiguous", candidates=[
            {"domain": "b.com", "score": 2, "qid": "Qb"},
            {"domain": "a.com", "score": 4, "qid": "Qa"},
        ]),
    )
    monkeypatch.setattr(dw, "_run_wikipedia", lambda n, f: _stage("no_match"))
    monkeypatch.setattr(
        dw, "_run_gkg",
        lambda n, b: _stage("ambiguous", candidates=[
            {"domain": "b.com", "score": 3, "entity_id": "c-b"},
            {"domain": "b.com", "score": 1, "entity_id": "c-b-dup"},
        ]),
    )
    monkeypatch.setattr(
        dw, "_run_ddg",
        lambda n, f, clock, sleep: _stage("ambiguous", candidates=[
            {"domain": "c.com", "url": "https://c.com", "score": 4, "title": "C", "position": 1},
        ]),
    )

    out = dw.discover_waterfall("Acme", object(), ddg_enabled=True)

    merged = out["candidates"]
    # Rank: score desc, then stage priority wikidata>wikipedia>gkg>ddg.
    assert [(c["domain"], c["score"], c["stage"]) for c in merged] == [
        ("a.com", 4, "wikidata"),
        ("c.com", 4, "ddg"),
        ("b.com", 3, "gkg"),
        ("b.com", 2, "wikidata"),
    ]
    # Dedup within a stage: gkg's second b.com entry is dropped; nothing fabricated.
    assert len(merged) == 4
    assert all("stage" in c for c in merged)


def test_domainless_candidates_kept_not_collapsed(monkeypatch):
    from src.identity import discover as dw

    monkeypatch.setattr(
        dw, "_run_wikidata",
        lambda n, f: _stage("no_match", candidates=[
            {"domain": None, "url": None, "score": 0, "qid": "Q1", "label": "One"},
            {"domain": None, "url": None, "score": 0, "qid": "Q2", "label": "Two"},
        ]),
    )
    monkeypatch.setattr(dw, "_run_wikipedia", lambda n, f: _stage("no_match"))
    monkeypatch.setattr(dw, "_run_gkg", lambda n, b: _stage("no_match"))

    out = dw.discover_waterfall("Acme", object())

    qids = [c.get("qid") for c in out["candidates"]]
    assert qids == ["Q1", "Q2"]  # both survive; no fabricated domain/url


# ── realistic generic-name ambiguity: Delta the airline vs Delta the faucet ─

def test_generic_delta_wikidata_ambiguous_no_waterfall_promotion(monkeypatch):
    """Plan T5 test 1 — the canonical generic-name trap: "Delta" the airline
    vs "Delta" the faucet company. Realistic payloads run through the REAL
    Wikidata filter/pick (both entities survive: each is P856-bearing, the
    faucet label also carries the "company" term), so the stage comes back
    ambiguous with both ranked — and one ambiguous stage must never promote
    the waterfall: no first hit, no 2-source agreement for either apex,
    merged candidates are exactly the deduped union, nothing fabricated."""
    from src.identity.wikidata_ids import WikidataDomainResolver

    search_body = json.dumps(
        {"search": [
            {"id": "Q44", "label": "Delta Air Lines",
             "description": "major American airline"},
            {"id": "Q5240378", "label": "Delta Faucet Company",
             "description": "American plumbing fixtures manufacturer"},
        ]}
    ).encode()
    claims_bodies = {
        "entity=Q44": json.dumps(
            {"claims": {"P856": [
                {"mainsnak": {"datavalue": {"value": "https://www.delta.com/"}}}
            ]}}
        ).encode(),
        "entity=Q5240378": json.dumps(
            {"claims": {"P856": [
                {"mainsnak": {"datavalue": {"value": "https://www.deltafaucet.com/"}}}
            ]}}
        ).encode(),
    }

    class Fetch:
        def get(self, task, **kw):
            if "wbsearchentities" in task.url:
                return SimpleNamespace(ok=True, doc=SimpleNamespace(body=search_body))
            for key, body in claims_bodies.items():
                if key in task.url:
                    return SimpleNamespace(ok=True, doc=SimpleNamespace(body=body))
            return SimpleNamespace(ok=False, doc=None)

    # Stage contract on the realistic payload: ambiguous, both ranked (tied
    # at 4 — both name-match their own apex), search order kept on the tie.
    stage = WikidataDomainResolver(Fetch(), None).discover("Delta")
    assert stage["status"] == "ambiguous"
    assert stage["domain"] is None
    assert [(c["qid"], c["domain"]) for c in stage["candidates"]] == [
        ("Q44", "delta.com"),
        ("Q5240378", "deltafaucet.com"),
    ]
    assert [c["score"] for c in stage["candidates"]] == [4, 4]

    # Waterfall end state: the ambiguous stage alone promotes nothing.
    from src.identity import discover as dw

    monkeypatch.setattr(dw, "_run_wikidata", lambda n, f: stage)
    monkeypatch.setattr(dw, "_run_wikipedia", lambda n, f: _stage("no_match"))
    monkeypatch.setattr(dw, "_run_gkg", lambda n, b: _stage("no_match"))

    out = dw.discover_waterfall("Delta", object())

    assert out["status"] == "ambiguous"
    assert out["domain"] is None
    assert "agreement" not in out
    assert out["stages"]["wikidata"]["status"] == "ambiguous"
    assert out["stages"]["wikipedia"]["status"] == "no_match"
    assert out["stages"]["gkg"]["status"] == "no_match"
    # Merged list = the (stage, domain) deduped union: both candidates once
    # each, stage-attributed, no fabricated entries.
    assert [(c["qid"], c["domain"], c["stage"]) for c in out["candidates"]] == [
        ("Q44", "delta.com", "wikidata"),
        ("Q5240378", "deltafaucet.com", "wikidata"),
    ]


# ── composer: real GKG stage (credential-gated no-op) ───────────────────────

def test_gkg_unconfigured_recorded_and_non_fatal(monkeypatch):
    from src.identity import discover as dw

    # No Google creds in the test env: the real client no-ops "unconfigured".
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.delenv("GKG_PROJECT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_KGSEARCH_KEY", raising=False)
    monkeypatch.setattr(dw, "_run_wikidata", lambda n, f: _stage("no_match"))
    monkeypatch.setattr(dw, "_run_wikipedia", lambda n, f: _stage("no_match"))

    out = dw.discover_waterfall("Acme", object())

    assert out["stages"]["gkg"]["status"] == "unconfigured"
    assert out["stages"]["gkg"]["candidates"] == []
    # Non-fatal: the waterfall finished normally on the remaining stages.
    assert out["status"] == "no_match"


# ── Orchestrator.discover: persistence into identity_candidates ─────────────

def test_orchestrator_discover_queues_ambiguous_run(tmp_path, monkeypatch):
    from src.identity import discover as dw
    from tests.test_orchestrator import _orch

    seen = {}

    def fake_waterfall(name, fetcher, *, gkg_backend="ekg", ddg_enabled=False,
                       ddg_fetcher=None, clock=None, sleep=None):
        seen.update(name=name, fetcher=fetcher, ddg=ddg_enabled, backend=gkg_backend)
        return {
            "name": name,
            "status": "ambiguous",
            "domain": None,
            "candidates": [
                {"domain": "acme.com", "score": 3, "stage": "wikidata"},
                {"domain": "acme.org", "score": 1, "stage": "gkg"},
            ],
            "stages": {"wikidata": {"status": "ambiguous"}, "gkg": {"status": "ambiguous"}},
        }

    monkeypatch.setattr(dw, "discover_waterfall", fake_waterfall)
    orch = _orch(tmp_path)

    out = orch.discover("Acme Corp")

    assert out["queued"] is True
    assert seen["name"] == "Acme Corp"
    assert seen["fetcher"] is orch.fetcher
    assert seen["backend"] == "ekg"
    assert seen["ddg"] is False
    row = orch.db.one("SELECT * FROM identity_candidates")
    # Wave-1 review fix: the store key is the normalize_entity form (the
    # table's documented contract) so "Acme Inc"/"acme" variants share a row.
    assert row["name"] == "acme"
    assert row["kind"] == "domain"
    assert row["source"] == "discover"
    assert row["status"] == "pending"
    assert json.loads(row["candidates_json"]) == [
        {"domain": "acme.com", "score": 3, "stage": "wikidata"},
        {"domain": "acme.org", "score": 1, "stage": "gkg"},
    ]


def test_orchestrator_discover_resolved_with_alternates_still_queued(tmp_path, monkeypatch):
    from src.identity import discover as dw
    from tests.test_orchestrator import _orch

    monkeypatch.setattr(
        dw, "discover_waterfall",
        lambda name, fetcher, **kw: {
            "name": name,
            "status": "resolved",
            "domain": "acme.com",
            "candidates": [
                {"domain": "acme.com", "score": 4, "stage": "wikidata"},
                {"domain": "acme.org", "score": 2, "stage": "ddg"},
            ],
            "stages": {"wikidata": {"status": "resolved"}},
        },
    )
    orch = _orch(tmp_path)

    out = orch.discover("Acme")

    assert out["status"] == "resolved"
    assert out["domain"] == "acme.com"  # resolved domain still returned for immediate use
    assert out["queued"] is True  # alternates queued for human review
    row = orch.db.one("SELECT * FROM identity_candidates")
    assert row is not None
    assert len(json.loads(row["candidates_json"])) == 2


def test_orchestrator_discover_resolved_without_candidates_not_queued(tmp_path, monkeypatch):
    from src.identity import discover as dw
    from tests.test_orchestrator import _orch

    monkeypatch.setattr(
        dw, "discover_waterfall",
        lambda name, fetcher, **kw: {
            "name": name, "status": "resolved", "domain": "acme.com",
            "candidates": [], "stages": {"wikidata": {"status": "resolved"}},
        },
    )
    orch = _orch(tmp_path)

    out = orch.discover("Acme")

    assert out["queued"] is False
    assert orch.db.one("SELECT * FROM identity_candidates") is None


def test_orchestrator_discover_ddg_flag_forwarded(tmp_path, monkeypatch):
    from src.identity import discover as dw
    from tests.test_orchestrator import _orch

    seen = {}

    def fake_waterfall(name, fetcher, *, gkg_backend="ekg", ddg_enabled=False,
                       ddg_fetcher=None, clock=None, sleep=None):
        seen["ddg"] = ddg_enabled
        return {"name": name, "status": "no_match", "domain": None,
                "candidates": [], "stages": {}}

    monkeypatch.setattr(dw, "discover_waterfall", fake_waterfall)
    orch = _orch(tmp_path)

    orch.discover("Acme", ddg=True)
    assert seen["ddg"] is True


# ── end-to-end: orchestrator -> real waterfall -> real Wikidata resolver ────

def test_orchestrator_discover_end_to_end_wikidata_first_hit(tmp_path):
    from tests.test_orchestrator import _orch

    search_body = json.dumps(
        {"search": [{"id": "Q42", "label": "Acme Corp", "description": "American software company"}]}
    ).encode()
    claims_body = json.dumps(
        {"claims": {"P856": [{"mainsnak": {"datavalue": {"value": "https://www.acme.com/"}}}]}}
    ).encode()

    class Fetch:
        def get(self, task, **kw):
            body = search_body if "wbsearchentities" in task.url else claims_body
            return SimpleNamespace(ok=True, doc=SimpleNamespace(body=body))

    orch = _orch(tmp_path, fetcher=Fetch())

    out = orch.discover("Acme Corp")

    assert out["status"] == "resolved"
    assert out["domain"] == "acme.com"
    assert out["queued"] is True
    assert out["stages"]["wikipedia"] == {"status": "skipped"}
    assert out["stages"]["gkg"] == {"status": "skipped"}
    row = orch.db.one("SELECT * FROM identity_candidates WHERE name = 'acme'")
    cands = json.loads(row["candidates_json"])
    assert cands[0]["domain"] == "acme.com"
    assert cands[0]["stage"] == "wikidata"
