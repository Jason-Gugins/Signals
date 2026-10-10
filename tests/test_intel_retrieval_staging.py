"""Task 9: the intel seam — retrieval-selected document staging.

Four contracts (the plan's Step 1, mirrored on the test_intel_llm_layer
bootstrap — helpers are imported from it, not reinvented):

1. Off-path byte-identity. Without the retrieval block enabled, staging is
   the exact recency tail (sorted by fetched_at, ``MAX_IMPLEMENT_DOCS``
   cap), ``select_stage_docs`` is NEVER called (monkeypatched to raise),
   and the sub-stage result carries no ``retrieval`` key.
2. Enabled + covered: the staged docs follow ``select_stage_docs`` order,
   the result carries ``staged_by='retrieval'`` + docs/coverage/provenance,
   and the implement loop receives the SAME (doc, text) tuples in that
   order.
3. Enabled + coverage below the floor: the recency tail comes back AND a
   gap names the coverage numbers and the remediation command
   (``python -m src.cli index --domain <domain>``).
4. ``select_stage_docs`` raising: recency fallback + gap line, the pass
   completes.

Plus a ``_retrieval_cfg`` unit pin and one run_intel-level wiring test
(the new ``cfg``/``market_profile`` kwargs actually reach the seam). The
seam's retrieval imports are lazy INSIDE the enabled branch, so the doubles
monkeypatch module attributes on ``src.retrieval.search`` /
``src.retrieval.embedder`` / ``src.signals.rerank`` — the from-import
resolves them at call time. Nothing imports a model or touches the network.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from types import SimpleNamespace

import respx

import src.pipeline.intel as intel_mod
from src.core.config import Config, RetrievalConfig
from src.core.models import Account
from src.decide.gates import DecideLedger
from src.decide.jev import DATA_EXIT_CONSENT
from src.retrieval import embedder as retrieval_embedder
from src.retrieval import search as retrieval_search
from src.retrieval.search import StageSelection
from src.signals import rerank as rerank_mod
from tests.test_intel_llm_layer import (
    DOMAIN,
    TAX,
    FakeOrchLlm,
    _FakeRawStore,
    _FakeSignalStore,
    _decide_cfg,
    _doc,
    _high_decider,
    _patch_adapters,
    _patch_coverage,
    _patch_layer,
    _patch_package_recorder,
    _patch_planner,
)


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _RecordingRawStore(_FakeRawStore):
    """_FakeRawStore that records the order bodies were loaded in — the
    implement pass loads one body per STAGED doc through get(), so the
    get() order IS the staged order."""

    def __init__(self, docs=()):
        super().__init__(docs)
        self.get_order: list[str] = []

    def get(self, doc_id):
        self.get_order.append(doc_id)
        return super().get(doc_id)


#: Three docs inserted NEWEST-FIRST so the recency tail's SORT is observable
#: (the store's dict order must not leak into staging).
DOCS_NEWEST_FIRST = [
    _doc("doc-c", "Gamma body text three.", fetched_at="2026-08-01T00:00:00+00:00"),
    _doc("doc-a", "Alpha body text one.", fetched_at="2026-06-01T00:00:00+00:00"),
    _doc("doc-b", "Beta body text two.", fetched_at="2026-07-01T00:00:00+00:00"),
]
RECENCY_ORDER = ["doc-a", "doc-b", "doc-c"]


def _staging_decide_cfg() -> dict:
    """decide.yaml-shaped cfg: a bulk model so the implement loop runs, with
    every Jev gate disabled so the pass runs decider-free — staging is what
    these tests pin, not the gates."""
    return {
        "implementers": {"bulk": {"model": "test/bulk", "base_url": "https://impl.test/v1"}},
        "decider": {
            "gates": {
                "routing": {"enabled": False},
                "citation_soundness": {"enabled": False},
                "need_promotion": {"enabled": False},
            }
        },
    }


def _patch_selection(monkeypatch, *, selection=None, exc=None):
    """Point the seam's lazy from-imports at test doubles.

    Returns (calls, embedder, scorer): ``calls`` records every kwarg the
    seam threaded into select_stage_docs; embedder/scorer are the exact
    objects get_embedder()/get_scorer() handed it."""
    calls: list[dict] = []
    fake_embedder = SimpleNamespace(model="fake@1")
    fake_scorer = SimpleNamespace(name="fake-scorer")

    def fake_select(db, domain, *, query, ce_query, embedder, scorer, cfg):
        calls.append(
            {
                "db": db,
                "domain": domain,
                "query": query,
                "ce_query": ce_query,
                "embedder": embedder,
                "scorer": scorer,
                "cfg": dict(cfg),
            }
        )
        if exc is not None:
            raise exc
        return selection

    monkeypatch.setattr(retrieval_search, "select_stage_docs", fake_select)
    monkeypatch.setattr(retrieval_embedder, "get_embedder", lambda: fake_embedder)
    monkeypatch.setattr(rerank_mod, "get_scorer", lambda: fake_scorer)
    return calls, fake_embedder, fake_scorer


def _patch_bulk(monkeypatch):
    """Implementer recorder capturing WHICH doc each bulk call extracts from
    (the reasoning implementer's calls are answered but not counted)."""
    rec: dict = {"bulk": []}

    def fake_call_implementer(
        messages, *, model, base_url, api_key, reasoning_effort=None, timeout_s=60.0
    ):
        if "REASONING IMPLEMENTER" in messages[0]["content"]:
            return {}
        doc_id = messages[1]["content"].split("Document doc_id: ", 1)[1].splitlines()[0].strip()
        rec["bulk"].append(doc_id)
        return {"claims": []}

    monkeypatch.setattr(intel_mod.llm_implement, "call_implementer", fake_call_implementer)
    return rec


def _run_pass(orch, *, cfg=None):
    """One _implement_pass over the fake orch (the level test_intel_llm_layer
    pins the layer's internals at — cheaper than a full run_intel)."""
    return intel_mod._implement_pass(
        orc=orch,
        account=Account(domain=DOMAIN, name="Acme"),
        domain=DOMAIN,
        decider=None,
        decide_cfg=_staging_decide_cfg(),
        ledger=DecideLedger(),
        run_id="run-test",
        mode="enforce",
        cfg=cfg if cfg is not None else Config(),
        market_profile=None,
    )


def _orc(docs):
    store = _RecordingRawStore(docs)
    return SimpleNamespace(
        raw=store, db=object(), taxonomy=TAX, signal_store=_FakeSignalStore()
    )


def _enabled_cfg(**overrides) -> Config:
    cfg = Config()
    cfg.retrieval = replace(RetrievalConfig(enabled=True), **overrides)
    return cfg


# ---------------------------------------------------------------------------
# 1. Off path: byte-identity with the recency tail
# ---------------------------------------------------------------------------


def test_off_path_is_byte_identical_recency_tail(monkeypatch):
    """No enabled retrieval block -> select_stage_docs is NEVER called (it is
    monkeypatched to raise AssertionError: even one call fails the test), the
    staged docs are the exact recency tail in fetched_at order, and the
    sub-stage result carries no `retrieval` key."""
    for cfg in (Config(), _enabled_cfg(enabled=False)):
        orch = _orc(DOCS_NEWEST_FIRST)
        rec = _patch_bulk(monkeypatch)
        calls, _emb, _sc = _patch_selection(
            monkeypatch, exc=AssertionError("must not be called")
        )

        result = _run_pass(orch, cfg=cfg)

        assert calls == []
        assert orch.raw.get_order == RECENCY_ORDER, "staging must sort by fetched_at"
        assert rec["bulk"] == RECENCY_ORDER, "implement loop sees the recency order"
        assert result["docs"] == 3
        assert "retrieval" not in result
        assert not any("retrieval" in gap for gap in result["gaps"])


# ---------------------------------------------------------------------------
# 2. Enabled + covered: retrieval order rules
# ---------------------------------------------------------------------------


def test_enabled_and_covered_stages_retrieval_order(monkeypatch):
    """enabled + staged_by='retrieval' -> the staged docs follow
    selection.doc_ids (recency order does NOT), the result carries
    staged_by/docs/coverage/provenance, and the implement loop receives the
    SAME (doc, text) tuples in that order."""
    orch = _orc(DOCS_NEWEST_FIRST)
    db_sentinel = orch.db
    selection = StageSelection(
        doc_ids=["doc-c", "doc-a"],
        coverage=1.0,
        staged_by="retrieval",
        provenance={"chunks_considered": 9, "legs": 2, "ce_scored": 1, "mmr_pool": 40, "mmr_k": 35},
    )
    calls, emb, sc = _patch_selection(monkeypatch, selection=selection)
    rec = _patch_bulk(monkeypatch)

    result = _run_pass(orch, cfg=_enabled_cfg())

    # staged order == selection order; doc-b never even loads a body
    assert orch.raw.get_order == ["doc-c", "doc-a"]
    assert rec["bulk"] == ["doc-c", "doc-a"]  # the (doc, text) tuples downstream
    assert result["docs"] == 2
    assert result["retrieval"]["staged_by"] == "retrieval"
    assert result["retrieval"]["docs"] == 2
    assert result["retrieval"]["coverage"] == 1.0
    assert result["retrieval"]["mmr_pool"] == 40  # provenance merged in
    assert result["retrieval"]["mmr_k"] == 35
    assert not any("retrieval" in gap for gap in result["gaps"])

    # the seam's plumbing: everything threaded, typed config -> plain dict
    (call,) = calls
    assert call["db"] is db_sentinel
    assert call["domain"] == DOMAIN
    assert call["embedder"] is emb
    assert call["scorer"] is sc
    assert call["cfg"] == asdict(RetrievalConfig(enabled=True))
    assert "acme" in call["query"].lower()  # real build_query: identity terms
    assert call["ce_query"]


# ---------------------------------------------------------------------------
# 3. Enabled + coverage below the floor: recency fallback + remediation gap
# ---------------------------------------------------------------------------


def test_enabled_uncovered_falls_back_to_recency_with_gap(monkeypatch):
    """enabled + coverage 0.5 < min_index_coverage -> the recency tail comes
    back AND a gap names the coverage, the staged_by and the exact
    remediation command."""
    orch = _orc(DOCS_NEWEST_FIRST)
    selection = StageSelection(doc_ids=[], coverage=0.5, staged_by="uncovered")
    _patch_selection(monkeypatch, selection=selection)
    rec = _patch_bulk(monkeypatch)

    result = _run_pass(orch, cfg=_enabled_cfg())

    assert orch.raw.get_order == RECENCY_ORDER  # recency fallback
    assert rec["bulk"] == RECENCY_ORDER
    assert result["docs"] == 3
    assert result["retrieval"]["staged_by"] == "uncovered"
    assert result["retrieval"]["coverage"] == 0.5
    gap = next(g for g in result["gaps"] if "index coverage" in g)
    assert "0.50" in gap
    assert DOMAIN in gap
    assert "uncovered" in gap
    assert f"python -m src.cli index --domain {DOMAIN}" in gap
    assert "recency" in gap


def test_empty_retrieval_selection_still_sorted_and_capped(monkeypatch):
    """staged_by='retrieval' with EMPTY doc_ids is not a retrieval selection
    (a retrieval result that selected nothing carries no order to honor): the
    seam must normalize it so the recency sort AND the MAX_IMPLEMENT_DOCS cap
    still apply — the cap holds on every path (review finding)."""
    monkeypatch.setattr(intel_mod, "MAX_IMPLEMENT_DOCS", 2)
    orch = _orc(DOCS_NEWEST_FIRST)
    selection = StageSelection(doc_ids=[], coverage=1.0, staged_by="retrieval")
    _patch_selection(monkeypatch, selection=selection)
    rec = _patch_bulk(monkeypatch)

    # stage_docs lowered to match the patched cap (a stage_docs above
    # MAX_IMPLEMENT_DOCS would disable the retrieval block entirely).
    result = _run_pass(orch, cfg=_enabled_cfg(stage_docs=2))

    # Recency order, capped at the (patched) MAX_IMPLEMENT_DOCS.
    assert orch.raw.get_order == ["doc-b", "doc-c"]
    assert rec["bulk"] == ["doc-b", "doc-c"]
    assert result["docs"] == 2
    assert result["retrieval"]["staged_by"] == "unavailable"  # normalized
    assert result["retrieval"]["coverage"] == 1.0
    assert any("recency" in gap for gap in result["gaps"]), result["gaps"]


# ---------------------------------------------------------------------------
# 4. select_stage_docs raising: recency fallback, the pass completes
# ---------------------------------------------------------------------------


def test_selection_exception_degrades_to_recency(monkeypatch):
    """select_stage_docs raising -> recency tail + gap line naming the error;
    the pass never aborts."""
    orch = _orc(DOCS_NEWEST_FIRST)
    _patch_selection(monkeypatch, exc=RuntimeError("index exploded"))
    rec = _patch_bulk(monkeypatch)

    result = _run_pass(orch, cfg=_enabled_cfg())

    assert orch.raw.get_order == RECENCY_ORDER  # recency fallback
    assert rec["bulk"] == RECENCY_ORDER
    assert result["docs"] == 3  # the pass completed
    assert result["retrieval"]["staged_by"] == "error"
    assert "index exploded" in result["retrieval"]["error"]
    assert any(
        "retrieval staging failed" in gap and "index exploded" in gap
        for gap in result["gaps"]
    ), result["gaps"]


# ---------------------------------------------------------------------------
# 5. _retrieval_cfg: the never-add-spend gate
# ---------------------------------------------------------------------------


def test_retrieval_cfg_gates_the_block():
    assert intel_mod._retrieval_cfg(Config()) is None  # block off by default
    assert intel_mod._retrieval_cfg(None) is None  # no config object at all
    cfg = _enabled_cfg()
    assert intel_mod._retrieval_cfg(cfg) is cfg.retrieval  # the TYPED block passes
    # stage_docs above MAX_IMPLEMENT_DOCS can never ADD spend: off
    cfg.retrieval = replace(cfg.retrieval, stage_docs=intel_mod.MAX_IMPLEMENT_DOCS + 1)
    assert intel_mod._retrieval_cfg(cfg) is None


# ---------------------------------------------------------------------------
# 6. run_intel wiring: cfg + market_profile actually reach the seam
# ---------------------------------------------------------------------------


@respx.mock
def test_run_intel_threads_cfg_into_the_seam(monkeypatch):
    """The run_intel call site passes the config through: enabled -> the
    stage record carries retrieval.staged_by; the SAME run with the block
    disabled (or absent) records no retrieval key and never calls
    select_stage_docs."""
    orch = FakeOrchLlm(
        docs=[
            _doc("doc-1", "Acme body one.", fetched_at="2026-08-15T00:00:00+00:00"),
            _doc("doc-2", "Acme body two.", fetched_at="2026-08-15T01:00:00+00:00"),
        ]
    )
    orch.db = object()  # the seam hands orc.db to select_stage_docs
    cfg = orch.config
    cfg.retrieval = replace(RetrievalConfig(enabled=True))
    _patch_coverage(monkeypatch)
    _patch_package_recorder(monkeypatch)
    _patch_layer(monkeypatch, decide_cfg=_decide_cfg("enforce"), decider=_high_decider())
    _patch_adapters(monkeypatch)
    _patch_planner(monkeypatch)
    rec = _patch_bulk(monkeypatch)
    selection = StageSelection(
        doc_ids=["doc-2", "doc-1"], coverage=1.0, staged_by="retrieval", provenance={"legs": 2}
    )
    calls, _emb, _sc = _patch_selection(monkeypatch, selection=selection)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", DATA_EXIT_CONSENT)

    result = intel_mod.run_intel(DOMAIN, config=cfg, orch=orch, with_llm=True)

    assert result["stages"]["implement"]["status"] == "ran"
    assert result["stages"]["implement"]["retrieval"]["staged_by"] == "retrieval"
    assert rec["bulk"] == ["doc-2", "doc-1"]  # retrieval order reached the loop
    assert len(calls) == 1 and calls[0]["domain"] == DOMAIN

    # The off path at the SAME level: fresh orch, block disabled by default —
    # no retrieval key, and the selection double is never called again.
    orch_off = FakeOrchLlm(docs=[_doc("doc-1", "Acme body one.")])
    result_off = intel_mod.run_intel(
        DOMAIN, config=orch_off.config, orch=orch_off, with_llm=True
    )

    assert result_off["stages"]["implement"]["status"] == "ran"
    assert "retrieval" not in result_off["stages"]["implement"]
    assert len(calls) == 1
    assert rec["bulk"] == ["doc-2", "doc-1", "doc-1"]  # the off run staged doc-1 only
    assert respx.calls.call_count == 0
