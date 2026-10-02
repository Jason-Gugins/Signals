"""Task 7: the decide layer wired into run_intel behind --with-llm.

Two contracts live in this file:

1. BYTE-IDENTITY (the off path). With ``--with-llm`` absent and the shipped
   ``config/decide.yaml`` mode "off", run_intel produces EXACTLY today's
   flow: no ``plan``/``implement`` stage entries, no layer gap lines, no
   ``decide`` key in the return dict, no decide kwargs on the package seams,
   and ONE primary collect call with ``sources=None``. The existing intel
   test files pin the same thing from their side and stay unmodified.

2. The layer-active path. Plan sub-stage (G1-qualified narrowing applied at
   collect), implement sub-stage (G3 route -> bulk claims -> G4 citations ->
   G5 promotion; five G4-gated dossier fields), the G2 posture audit, the
   per-run decisions.jsonl and the dossier's decide evidence. Every LLM and
   Jev seam is monkeypatched, so no test spends a token or touches the
   network (respx asserts zero HTTP calls on the layer-active paths).

Failure-posture checks (missing credentials, token-budget overrun) pin the
degradation gap lines: the layer can never crash the run.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import respx
import yaml
from click.testing import CliRunner

import src.cli as cli_mod
import src.pipeline.intel as intel_mod
from src.core.config import Config
from src.core.models import Account, Document
from src.decide import Decision, MockDecider, NullDecider, Plan, PlanStep
from src.decide.jev import DATA_EXIT_CONSENT
from src.intel.snapshot import build_intelligence_snapshot
from src.pipeline import intel
from src.pipeline.runner import RunnerStats
from src.signals.lifecycle import load_supersede_map
from src.signals.taxonomy import Taxonomy

DOMAIN = "acme.com"

TODAY = date(2026, 8, 16)
TAX = Taxonomy.load("config/signals.yaml")
SIGNALS_CFG = yaml.safe_load(Path("config/signals.yaml").read_text(encoding="utf-8"))
SUPERSEDE = load_supersede_map(SIGNALS_CFG)
SCORING = yaml.safe_load(Path("config/scoring.yaml").read_text(encoding="utf-8"))
PLAYS = yaml.safe_load(Path("config/plays.yaml").read_text(encoding="utf-8"))

#: A document body with a mid-document anchor the fake implementer's claim
#: quotes verbatim (the G4 lexical prefilter needs the overlap).
DOC_TEXT_1 = (
    "Acme company blog. "
    "Acme is evaluating SIEM vendors after a breach last quarter. "
    "The platform team plans to consolidate three monitoring tools into one."
)
CLAIM_TEXT = "Acme is evaluating SIEM vendors after a breach last quarter."
WHY_NOW_TEXT = "A breach last quarter drives urgency."

FIVE_FIELDS = {
    "operational_need": {"text": CLAIM_TEXT, "doc_id": "doc-1"},
    "why_now": {"text": WHY_NOW_TEXT, "doc_id": "doc-1"},
}


# ---------------------------------------------------------------------------
# Recording doubles (FakeOrch pattern from tests/test_intel.py, extended with
# the raw store, the signal store and the taxonomy the implement sub-stage
# reads)
# ---------------------------------------------------------------------------


class _Registry:
    def __init__(self, accounts=None):
        self.accounts = dict(accounts or {})

    def get(self, domain):
        return self.accounts.get(domain)

    def upsert(self, account, *, source=None):
        self.accounts[account.domain] = account
        return account


class _FakeRawStore:
    """RawStore stand-in over an in-memory document list.

    Mirrors the REAL store contract (src/core/rawstore.py): iter_docs yields
    METADATA-ONLY Documents — ``body`` stays None because only get() gunzips
    the stored bytes — so a consumer that reads bodies straight off
    iter_docs decodes nothing and can never be masked by this fake.
    """

    def __init__(self, docs=()):
        self._docs = {doc.doc_id: doc for doc in docs}

    def iter_docs(self, *, source=None, domain=None, since=None, unparsed_only=False):
        for doc in self._docs.values():
            if source and doc.source != source:
                continue
            if domain and doc.domain != domain:
                continue
            if since and (doc.fetched_at or "") < since:
                continue
            if unparsed_only and doc.parsed_at is not None:
                continue
            yield replace(doc, body=None)  # metadata only, like the real store

    def get(self, doc_id):
        """The body-carrying Document (or None when the id is unknown)."""
        return self._docs.get(doc_id)


class _FakeSignalStore:
    """Records every upsert so tests can pin promotion vs shadow."""

    def __init__(self):
        self.upserted = []

    def upsert_many(self, signals):
        self.upserted.extend(signals)
        return len(signals), 0


class _FakeAdapter:
    """Just enough adapter for ``sources_for_account`` (requires: ())."""

    def __init__(self, key, *, fanout=False):
        self.key = key
        self.fanout = fanout
        self.requires = ()
        self.tier = "http"


class FakeOrchLlm:
    """Fake orchestrator: records resolve/collect/score, serves raw docs."""

    def __init__(self, *, accounts=None, snapshot=None, docs=()):
        self.registry = _Registry(accounts or {DOMAIN: Account(domain=DOMAIN, name="Acme")})
        self.config = Config()
        self.calls = []
        self.snapshot = snapshot
        self.raw = _FakeRawStore(docs)
        self.signal_store = _FakeSignalStore()
        self.taxonomy = TAX

    def resolve(self, **kw):
        self.calls.append(("resolve", kw))
        return {"accounts": 1}

    def collect(self, **kw):
        self.calls.append(("collect", kw))
        stats = RunnerStats()
        stats.tasks = 2
        stats.signals_new = 1
        stats.mark("dummy_source", DOMAIN, "ran_data")
        return stats

    def score(self, **kw):
        self.calls.append(("score", kw))
        domain = list(kw["domains"])[0]
        snap = self.snapshot
        if snap is None:
            snap = SimpleNamespace(domain=domain, today=TODAY)
        if kw.get("return_snapshots"):
            return {"scored": 1, "snapshots": {domain: snap}}
        return {"scored": 1}


def _doc(doc_id, text, fetched_at="2026-08-15T00:00:00+00:00"):
    return Document(
        doc_id=doc_id,
        source="company_feed",
        url=f"https://acme.com/{doc_id}",
        domain=DOMAIN,
        fetched_at=fetched_at,
        body=text.encode("utf-8"),
    )


def _snapshot():
    """A REAL intelligence snapshot so the REAL build_dossier can run."""
    return build_intelligence_snapshot(
        account=Account(domain=DOMAIN, name="Acme", icp_fit=1.0),
        signals=[],
        taxonomy=TAX,
        scoring_cfg=SCORING,
        plays_cfg=PLAYS,
        icp_rules={},
        contacts=[],
        today=TODAY,
        supersede_days_by_type=SUPERSEDE,
    )


# ---------------------------------------------------------------------------
# Seam patches
# ---------------------------------------------------------------------------


def _decide_cfg(mode="enforce", **decider_overrides):
    """A decide.yaml-shaped dict (mirrors the shipped config's structure)."""
    decider = {
        "model": "jev-test",
        "max_decide_tokens_per_run": 400000,
        "gates": {
            "plan_qualification": {"enabled": True, "floor": 0.70},
            "posture_audit": {"enabled": True},
            "routing": {"enabled": True, "route_threshold": 2000},
            "citation_soundness": {"enabled": True, "floor": 0.70},
            "need_promotion": {"enabled": True, "floor": 0.70},
        },
    }
    decider.update(decider_overrides)
    return {
        "mode": mode,
        "planner": {
            "model": "test/planner",
            "base_url": "https://planner.test/v1",
            "max_plan_steps": 4,
            "plan_cache": False,
        },
        "implementers": {
            "bulk": {"model": "test/bulk", "base_url": "https://impl.test/v1"},
            "reasoning": {
                "model": "test/reasoning",
                "base_url": "https://impl.test/v1",
                "reasoning_effort": "max",
            },
        },
        "decider": decider,
    }


def _high_decider():
    """MockDecider with high verdicts for every gate (exact ids + 'claim' prefix)."""
    claim_answers = {f"claim_{i}": {"noul": 0.9} for i in range(8)}
    return MockDecider(
        {
            "step": Decision(applies=True, ok=True, answers={"step": {"noul": 0.9}}, raw_tokens=10),
            "claim": Decision(applies=True, ok=True, answers=claim_answers, raw_tokens=40),
            "posture": Decision(
                applies=True, ok=True, answers={"posture": {"choice": "within_posture"}}, raw_tokens=5
            ),
            "routing": Decision(
                applies=True, ok=True, answers={"routing": {"choice": "quick"}}, raw_tokens=5
            ),
            "promotion": Decision(
                applies=True, ok=True, answers={"promotion": {"noul": 0.9}}, raw_tokens=10
            ),
        }
    )


class _RecordingDecider(MockDecider):
    """MockDecider that captures the state of every G5 promotion call."""

    def __init__(self, verdicts: dict[str, Decision]) -> None:
        super().__init__(verdicts)
        self.promotion_states: list = []

    def decide(self, state, questions):
        if "promotion" in questions:
            self.promotion_states.append(state)
        return super().decide(state, questions)


class _StepSelectiveDecider:
    """G1-only decider: drops steps naming ``drop_source``, keeps the rest.

    ``ok=False`` turns the dropped step's verdict into a Jev ERROR (the
    on_error fallback path) instead of a low-noul verdict.
    """

    def __init__(self, drop_source: str, *, ok: bool = True) -> None:
        self.drop_source = drop_source
        self.ok = ok

    def decide(self, state, questions):
        if "step" in questions:
            if self.drop_source in state["step"]["source_ids"]:
                if not self.ok:
                    return Decision(applies=True, ok=False, answers={}, raw_tokens=3)
                return Decision(
                    applies=True, ok=True, answers={"step": {"noul": 0.1}}, raw_tokens=5
                )
            return Decision(applies=True, ok=True, answers={"step": {"noul": 0.9}}, raw_tokens=5)
        return Decision(applies=False, ok=True, answers={}, raw_tokens=0)


def _patch_layer(monkeypatch, *, decide_cfg, decider):
    """Point the decide-layer seams at test doubles."""
    monkeypatch.setattr(intel, "_load_decide_cfg", lambda cfg: dict(decide_cfg))
    monkeypatch.setattr(intel, "get_decider", lambda cfg: decider)
    # attach_decider's scope guard refuses a MockDecider BY DESIGN (policy.py
    # turns any non-Live handle into a NullDecider). These tests pin the
    # wiring, not the guard — test_decide_policy.py owns the guard — so the
    # handle passes straight through here.
    monkeypatch.setattr(intel.decide_policy, "attach_decider", lambda handle: handle)


def _patch_coverage(monkeypatch):
    monkeypatch.setattr(intel, "build_coverage", lambda **kw: [])


def _patch_adapters(monkeypatch):
    """The planner's allowed scope comes from the adapter registry; fake it."""
    monkeypatch.setattr(
        intel,
        "enabled_sources",
        lambda cfg, include_disabled=None: [
            _FakeAdapter("news_rss"),
            _FakeAdapter("company_feed"),
            _FakeAdapter("sec_formd", fanout=True),
        ],
    )


def _patch_planner(monkeypatch, *, plan=None, reason="generated"):
    rec = {"calls": []}

    def fake_generate_plan(**kw):
        rec["calls"].append(kw)
        return (plan, reason)

    monkeypatch.setattr(intel.llm_planner, "generate_plan", fake_generate_plan)
    return rec


def _patch_implementer(monkeypatch, *, claims=None, fields=None):
    """Patch the ONE implementer HTTP seam; both the bulk pass and
    extract_five_fields go through llm_implement.call_implementer."""
    rec = {"calls": []}

    def fake_call_implementer(messages, *, model, base_url, api_key, reasoning_effort=None, timeout_s=60.0):
        rec["calls"].append({"model": model, "reasoning_effort": reasoning_effort})
        system = messages[0]["content"]
        if "REASONING IMPLEMENTER" in system:
            return fields
        return {"claims": claims}

    monkeypatch.setattr(intel.llm_implement, "call_implementer", fake_call_implementer)
    return rec


def _patch_package_recorder(monkeypatch):
    """Recorder doubles for build_dossier/write_intel_package (off-path tests)."""
    rec = {}

    def fake_build_dossier(snapshot, **kw):
        rec["dossier_kwargs"] = kw
        return {"domain": DOMAIN, "gaps": list(kw.get("gaps") or [])}

    def fake_write(dossier, *, out_dir, **kw):
        rec["write_kwargs"] = kw
        return {"package_dir": "pkg"}

    monkeypatch.setattr(intel, "build_dossier", fake_build_dossier)
    monkeypatch.setattr(intel, "write_intel_package", fake_write)
    return rec


def _collect_calls(orch):
    return [kw for name, kw in orch.calls if name == "collect"]


def _clear_env(monkeypatch):
    for name in ("OPENROUTER_API_KEY", "TYPESAFE_API_KEY", "SIGNALS_DECIDE_DATA_EXIT"):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# 1. The off path: byte-identity
# ---------------------------------------------------------------------------


def test_layer_off_is_byte_identical(monkeypatch):
    orch = FakeOrchLlm()
    _patch_coverage(monkeypatch)
    rec = _patch_package_recorder(monkeypatch)

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, with_llm=False)
    orch_again = FakeOrchLlm()
    again = intel.run_intel(DOMAIN, config=orch_again.config, orch=orch_again)  # kwarg absent

    stages = result["stages"]
    assert list(stages) == list(intel.STAGES)
    assert "plan" not in stages and "implement" not in stages
    assert "decide" not in result
    assert result["gaps"] == again["gaps"]
    assert not any("llm layer" in gap or "decide" in gap for gap in result["gaps"])
    # exactly the primary + derive collects; the primary stays sources=None
    collects = _collect_calls(orch)
    assert len(collects) == 2
    assert collects[0]["sources"] is None
    # the package seams receive NO decide kwargs (old signatures still fit)
    assert "decide_records" not in rec["dossier_kwargs"]
    assert "decide_meta" not in rec["dossier_kwargs"]
    assert "llm_fields" not in rec["dossier_kwargs"]
    assert "decide_rows" not in rec["write_kwargs"]


def test_layer_off_never_touches_the_llm_or_decide_seams(monkeypatch):
    orch = FakeOrchLlm(docs=[_doc("doc-1", DOC_TEXT_1)])
    _patch_coverage(monkeypatch)
    _patch_package_recorder(monkeypatch)
    planner_rec = _patch_planner(monkeypatch)
    impl_rec = _patch_implementer(monkeypatch)

    intel.run_intel(DOMAIN, config=orch.config, orch=orch)

    assert planner_rec["calls"] == []
    assert impl_rec["calls"] == []
    assert orch.signal_store.upserted == []


def test_layer_requested_but_mode_off_records_gap_and_stays_deterministic(monkeypatch):
    orch = FakeOrchLlm()
    _patch_coverage(monkeypatch)
    _patch_package_recorder(monkeypatch)
    planner_rec = _patch_planner(monkeypatch)
    _patch_layer(monkeypatch, decide_cfg=_decide_cfg("off"), decider=NullDecider())

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, with_llm=True)

    assert any("decide mode is off" in gap for gap in result["gaps"])
    assert list(result["stages"]) == list(intel.STAGES)
    assert "decide" not in result
    collects = _collect_calls(orch)
    assert collects[0]["sources"] is None
    assert planner_rec["calls"] == []


def test_mode_on_without_the_flag_records_the_not_requested_gap(monkeypatch):
    orch = FakeOrchLlm()
    _patch_coverage(monkeypatch)
    _patch_package_recorder(monkeypatch)
    planner_rec = _patch_planner(monkeypatch)
    _patch_layer(monkeypatch, decide_cfg=_decide_cfg("enforce"), decider=NullDecider())

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch)  # no --with-llm

    assert any("llm layer not requested" in gap for gap in result["gaps"])
    # that gap line is the ONLY layer-off side effect
    assert list(result["stages"]) == list(intel.STAGES)
    assert "decide" not in result
    assert _collect_calls(orch)[0]["sources"] is None
    assert planner_rec["calls"] == []


# ---------------------------------------------------------------------------
# 2. Layer active, no credentials: every role degrades, run completes
# ---------------------------------------------------------------------------


@respx.mock
def test_layer_active_without_credentials_degrades_softly(monkeypatch):
    orch = FakeOrchLlm(docs=[_doc("doc-1", DOC_TEXT_1)])
    _patch_coverage(monkeypatch)
    _patch_package_recorder(monkeypatch)
    _patch_layer(monkeypatch, decide_cfg=_decide_cfg("enforce"), decider=NullDecider())
    _patch_adapters(monkeypatch)
    _clear_env(monkeypatch)  # real planner/implementer seams, zero credentials

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, with_llm=True)

    assert any(
        "llm planner unavailable (missing_credentials)" in gap for gap in result["gaps"]
    ), result["gaps"]
    assert any(
        "llm implementers unavailable (missing OPENROUTER_API_KEY or data-exit consent)"
        in gap
        for gap in result["gaps"]
    ), result["gaps"]
    # deterministic collect unchanged; the run completes
    collects = _collect_calls(orch)
    assert len(collects) == 2
    assert collects[0]["sources"] is None
    assert result["stages"]["plan"]["status"] == "ran"
    assert result["stages"]["plan"]["steps"] == 0
    assert result["stages"]["implement"]["status"] == "skipped"
    # the decider was a NullDecider: rows recorded but never called
    assert result["decide"]["mode"] == "enforce"
    assert result["decide"]["degraded"] is False
    assert result["decide"]["rows"] >= 1
    assert respx.calls.call_count == 0


# ---------------------------------------------------------------------------
# 3. Layer active, full flow in enforce mode
# ---------------------------------------------------------------------------


@respx.mock
def test_layer_active_enforce_full_flow(tmp_path, monkeypatch):
    orch = FakeOrchLlm(docs=[_doc("doc-1", DOC_TEXT_1)])
    orch.config.storage.dossiers_dir = str(tmp_path / "dossiers")
    orch.snapshot = _snapshot()
    _patch_coverage(monkeypatch)
    _patch_layer(monkeypatch, decide_cfg=_decide_cfg("enforce"), decider=_high_decider())
    _patch_adapters(monkeypatch)
    # the implementer HTTP seam is patched; the env keys only gate the spend
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", DATA_EXIT_CONSENT)
    plan = Plan(
        steps=[
            PlanStep(
                source_ids=["news_rss"],
                budget_knobs={},
                acceptance_criteria=["every step cites a doc_id"],
            )
        ]
    )
    planner_rec = _patch_planner(monkeypatch, plan=plan, reason="generated")
    impl_rec = _patch_implementer(
        monkeypatch,
        claims=[{"text": CLAIM_TEXT, "doc_id": "doc-1"}],
        fields=FIVE_FIELDS,
    )

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, with_llm=True)

    # --- plan sub-stage -----------------------------------------------------
    stages = result["stages"]
    assert stages["plan"]["status"] == "ran"
    assert stages["plan"]["steps"] == 1
    assert stages["plan"]["applied_sources"] == ["news_rss"]
    assert stages["plan"]["knobs_applied"] is False
    assert stages["plan"]["reason"] == "generated"

    plan_call = planner_rec["calls"][0]
    assert plan_call["allowed_sources"] == {"news_rss", "company_feed", "sec_formd"}
    assert plan_call["fanout_keys"] == {"sec_formd"}
    assert plan_call["include_fanout"] is False
    assert plan_call["knob_caps"]["company_feed"] == {"article_follow_max": 5}  # real config
    assert len(plan_call["snapshot_hash"]) == 64
    assert "acme.com" in plan_call["icp_excerpt"]
    assert plan_call["profile"] == "default"

    # --- collect narrowed per surviving plan step ----------------------------
    collects = _collect_calls(orch)
    assert collects[0]["sources"] == ["news_rss"]
    assert collects[1]["sources"] == ["jobsignals", "needs"]  # derive unchanged

    # --- implement sub-stage --------------------------------------------------
    assert stages["implement"]["status"] == "ran"
    assert stages["implement"]["docs"] == 1
    assert stages["implement"]["claims_accepted"] == 1
    assert stages["implement"]["promoted"] == 1
    assert stages["implement"]["llm_fields"] is True
    assert impl_rec["calls"][0]["model"] == "test/bulk"
    assert any(call["model"] == "test/reasoning" and call["reasoning_effort"] == "max"
               for call in impl_rec["calls"])

    # the G5-promoted candidate reached the signal store
    assert len(orch.signal_store.upserted) == 1
    sig = orch.signal_store.upserted[0]
    assert sig.source == "llm_implement"
    assert sig.signal_type == "llm_need"
    assert sig.confidence == 0.9

    # --- provenance -----------------------------------------------------------
    assert result["decide"]["mode"] == "enforce"
    assert result["decide"]["degraded"] is False
    assert result["decide"]["rows"] > 0

    package_dir = Path(result["paths"]["package_dir"])
    decisions_path = Path(result["paths"]["decisions"])
    assert decisions_path.parent == package_dir
    rows = [json.loads(line) for line in decisions_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == result["decide"]["rows"]
    assert any(row["gate"] == "need_promotion" for row in rows)
    assert any(row["gate"] == "plan_qualification" for row in rows)
    assert any(row["gate"] == "posture_audit" for row in rows)

    dossier = json.loads((package_dir / "dossier.json").read_text(encoding="utf-8"))
    assert [r["kind"] for r in dossier["decide_records"]] == ["jev_decision"] * len(
        dossier["decide_records"]
    )
    assert any(r["signal_id"].startswith("decide-") for r in dossier["decide_records"])
    assert all(r["source"] == "decide" for r in dossier["decide_records"])
    assert dossier["decide_mode"] == "enforce"
    assert dossier["decide_degraded"] is False
    assert "need_promotion" in dossier["decide_shadow_appendix"]
    # S4: the five G4-gated LLM fields reach the dossier, audited shape.
    assert set(dossier["llm_fields"]) == {"operational_need", "why_now"}
    for field, entry in dossier["llm_fields"].items():
        assert entry["llm_authored"] is True
        assert entry["model"] == "test/reasoning"
        assert entry["doc_id"] == "doc-1"
        assert entry["text"]
    assert dossier["llm_fields"]["operational_need"]["text"] == CLAIM_TEXT
    assert dossier["llm_fields"]["why_now"]["text"] == WHY_NOW_TEXT

    manifest = json.loads((package_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["decide"]["mode"] == "enforce"
    assert "decide_degraded" not in manifest  # only written when truthy

    assert respx.calls.call_count == 0


# ---------------------------------------------------------------------------
# 4. Shadow mode: rows and appendix, never enforcement
# ---------------------------------------------------------------------------


@respx.mock
def test_layer_active_shadow_records_but_never_promotes(tmp_path, monkeypatch):
    orch = FakeOrchLlm(docs=[_doc("doc-1", DOC_TEXT_1)])
    orch.config.storage.dossiers_dir = str(tmp_path / "dossiers")
    orch.snapshot = _snapshot()
    _patch_coverage(monkeypatch)
    _patch_layer(monkeypatch, decide_cfg=_decide_cfg("shadow"), decider=_high_decider())
    _patch_adapters(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", DATA_EXIT_CONSENT)
    plan = Plan(steps=[PlanStep(source_ids=["news_rss"], budget_knobs={}, acceptance_criteria=[])])
    _patch_planner(monkeypatch, plan=plan, reason="generated")
    _patch_implementer(
        monkeypatch,
        claims=[{"text": CLAIM_TEXT, "doc_id": "doc-1"}],
        fields=FIVE_FIELDS,
    )

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, with_llm=True)

    # shadow never promotes: the signal store is untouched by this stage
    assert orch.signal_store.upserted == []
    assert result["decide"]["mode"] == "shadow"
    assert result["decide"]["degraded"] is False
    assert result["decide"]["rows"] > 0

    # S1: shadow NEVER binds the plan (invariant 5) — collect stays the single
    # deterministic full-scope call. The G1 rows still record what enforce
    # would do, and the stage record says the deterministic scope was kept.
    collects = _collect_calls(orch)
    assert len(collects) == 2
    assert collects[0]["sources"] is None
    assert collects[1]["sources"] == ["jobsignals", "needs"]
    assert result["stages"]["plan"]["steps"] == 1
    assert result["stages"]["plan"]["applied_sources"] == ["news_rss"]
    assert result["stages"]["plan"]["note"] == "shadow: deterministic scope retained"

    dossier = json.loads(
        Path(result["paths"]["package_dir"]).joinpath("dossier.json").read_text(encoding="utf-8")
    )
    assert dossier["decide_mode"] == "shadow"
    appendix = dossier["decide_shadow_appendix"]
    assert appendix["need_promotion"]["agree_directions"] == {"jev_yes_det_no": 1}
    assert appendix["citation_soundness"]["agree_rate"] == 1.0

    manifest = json.loads(
        Path(result["paths"]["package_dir"]).joinpath("manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["decide"]["mode"] == "shadow"
    assert respx.calls.call_count == 0


# ---------------------------------------------------------------------------
# 5. G1 drops: enforce falls back to deterministic collection, never removal;
#    shadow never binds the plan at all
# ---------------------------------------------------------------------------


def test_shadow_plan_never_binds_but_enforce_narrows_collect(monkeypatch):
    """S1: the SAME valid plan — shadow collects the deterministic full scope
    ONCE (sources=None); enforce narrows to one collect per surviving step."""
    plan = Plan(steps=[PlanStep(source_ids=["news_rss"], budget_knobs={}, acceptance_criteria=[])])
    for mode, expected_first in (("shadow", None), ("enforce", ["news_rss"])):
        orch = FakeOrchLlm()
        _patch_coverage(monkeypatch)
        _patch_package_recorder(monkeypatch)
        _patch_layer(monkeypatch, decide_cfg=_decide_cfg(mode), decider=_high_decider())
        _patch_adapters(monkeypatch)
        _patch_planner(monkeypatch, plan=plan, reason="generated")

        result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, with_llm=True)

        collects = _collect_calls(orch)
        assert collects[0]["sources"] == expected_first, mode
        assert collects[1]["sources"] == ["jobsignals", "needs"], mode  # derive unchanged
        assert result["stages"]["plan"]["steps"] == 1, mode


def test_g1_enforce_drops_all_steps_collects_them_deterministically(monkeypatch):
    """S2: a dropped step's sources are NEVER removed — they fall back to one
    deterministic collection phase, with a gap naming Jev."""
    low = MockDecider(
        {"step": Decision(applies=True, ok=True, answers={"step": {"noul": 0.1}}, raw_tokens=5)}
    )
    orch = FakeOrchLlm()
    _patch_coverage(monkeypatch)
    _patch_package_recorder(monkeypatch)
    _patch_layer(monkeypatch, decide_cfg=_decide_cfg("enforce"), decider=low)
    _patch_adapters(monkeypatch)
    plan = Plan(steps=[PlanStep(source_ids=["news_rss"], budget_knobs={}, acceptance_criteria=[])])
    _patch_planner(monkeypatch, plan=plan, reason="generated")

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, with_llm=True)

    assert result["stages"]["plan"]["steps"] == 0
    assert "no qualified steps" in result["stages"]["plan"]["note"]
    collects = _collect_calls(orch)
    assert collects[0]["sources"] == ["news_rss"]  # deterministic fallback, not removal
    assert collects[1]["sources"] == ["jobsignals", "needs"]  # derive unchanged
    assert any(
        "G1 dropped plan step (sources: news_rss) after Jev verdict/error (below floor)"
        in gap
        and "deterministic collection retained" in gap
        for gap in result["gaps"]
    ), result["gaps"]


def test_g1_enforce_dropped_step_falls_back_kept_step_in_plan_order(monkeypatch):
    """S2: enforce + 2 steps, step 1 dropped (low noul) => collect runs the
    kept step's sources first (plan order), then step 1's sources as one
    deterministic fallback phase, and the coverage gap is recorded."""
    decider = _StepSelectiveDecider("news_rss")
    orch = FakeOrchLlm()
    _patch_coverage(monkeypatch)
    _patch_package_recorder(monkeypatch)
    _patch_layer(monkeypatch, decide_cfg=_decide_cfg("enforce"), decider=decider)
    _patch_adapters(monkeypatch)
    plan = Plan(
        steps=[
            PlanStep(source_ids=["news_rss"], budget_knobs={}, acceptance_criteria=[]),
            PlanStep(source_ids=["company_feed"], budget_knobs={}, acceptance_criteria=[]),
        ]
    )
    _patch_planner(monkeypatch, plan=plan, reason="generated")

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, with_llm=True)

    collects = _collect_calls(orch)
    assert collects[0]["sources"] == ["company_feed"]  # surviving step, plan order
    assert collects[1]["sources"] == ["news_rss"]  # dropped step: deterministic fallback
    assert collects[2]["sources"] == ["jobsignals", "needs"]  # derive unchanged
    assert result["stages"]["plan"]["steps"] == 1
    assert result["stages"]["plan"]["applied_sources"] == ["company_feed"]
    assert any(
        "G1 dropped plan step (sources: news_rss) after Jev verdict/error (below floor)"
        in gap
        for gap in result["gaps"]
    ), result["gaps"]


def test_g1_jev_error_drop_also_falls_back_with_reason(monkeypatch):
    """S2: a Jev ERROR drop behaves like a floor drop — deterministic
    collection retained, with reason 'Jev error' in the gap."""
    decider = _StepSelectiveDecider("news_rss", ok=False)
    orch = FakeOrchLlm()
    _patch_coverage(monkeypatch)
    _patch_package_recorder(monkeypatch)
    _patch_layer(monkeypatch, decide_cfg=_decide_cfg("enforce"), decider=decider)
    _patch_adapters(monkeypatch)
    plan = Plan(
        steps=[
            PlanStep(source_ids=["news_rss"], budget_knobs={}, acceptance_criteria=[]),
            PlanStep(source_ids=["company_feed"], budget_knobs={}, acceptance_criteria=[]),
        ]
    )
    _patch_planner(monkeypatch, plan=plan, reason="generated")

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, with_llm=True)

    collects = _collect_calls(orch)
    assert collects[0]["sources"] == ["company_feed"]
    assert collects[1]["sources"] == ["news_rss"]
    assert any(
        "G1 dropped plan step (sources: news_rss) after Jev verdict/error (Jev error)"
        in gap
        for gap in result["gaps"]
    ), result["gaps"]


# ---------------------------------------------------------------------------
# 6. Token pre-flight: over ceiling degrades the whole run to shadow
# ---------------------------------------------------------------------------


@respx.mock
def test_token_budget_overrun_degrades_whole_run_to_shadow(tmp_path, monkeypatch):
    huge = "Acme breach quarter evaluating vendors. " * 300  # well over the cap
    orch = FakeOrchLlm(docs=[_doc("doc-1", huge)])
    orch.config.storage.dossiers_dir = str(tmp_path / "dossiers")
    orch.snapshot = _snapshot()
    _patch_coverage(monkeypatch)
    _patch_layer(
        monkeypatch,
        decide_cfg=_decide_cfg("enforce", max_decide_tokens_per_run=1000),
        decider=_high_decider(),
    )
    _patch_adapters(monkeypatch)
    plan = Plan(steps=[PlanStep(source_ids=["news_rss"], budget_knobs={}, acceptance_criteria=[])])
    _patch_planner(monkeypatch, plan=plan, reason="generated")
    _patch_implementer(
        monkeypatch,
        claims=[{"text": CLAIM_TEXT, "doc_id": "doc-1"}],
        fields=FIVE_FIELDS,
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", DATA_EXIT_CONSENT)

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, with_llm=True)

    assert any("decide token budget exceeded" in gap for gap in result["gaps"]), result["gaps"]
    assert result["decide"]["mode"] == "shadow"  # whole-run degradation
    assert result["decide"]["degraded"] is True
    # degraded to shadow => NO promotion, even though the enforce verdicts pass
    assert orch.signal_store.upserted == []

    manifest = json.loads(
        Path(result["paths"]["package_dir"]).joinpath("manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["decide_degraded"] is True
    dossier = json.loads(
        Path(result["paths"]["package_dir"]).joinpath("dossier.json").read_text(encoding="utf-8")
    )
    assert dossier["decide_degraded"] is True
    assert respx.calls.call_count == 0


# ---------------------------------------------------------------------------
# 6b. Enforce-mode Jev degradation is artifact-visible (invariant 3)
# ---------------------------------------------------------------------------


@respx.mock
def test_enforce_jev_error_flags_decide_degraded(tmp_path, monkeypatch):
    """S5: enforce + a Jev error at G4 => the run is flagged decide_degraded
    in the dossier AND the manifest, with a gap naming the fallbacks."""
    orch = FakeOrchLlm(docs=[_doc("doc-1", DOC_TEXT_1)])
    orch.config.storage.dossiers_dir = str(tmp_path / "dossiers")
    orch.snapshot = _snapshot()
    _patch_coverage(monkeypatch)
    g4_error = MockDecider(
        {
            "step": Decision(applies=True, ok=True, answers={"step": {"noul": 0.9}}, raw_tokens=10),
            "claim": Decision(applies=True, ok=False, answers={}, raw_tokens=0),
            "posture": Decision(
                applies=True, ok=True, answers={"posture": {"choice": "within_posture"}}, raw_tokens=5
            ),
            "routing": Decision(
                applies=True, ok=True, answers={"routing": {"choice": "quick"}}, raw_tokens=5
            ),
        }
    )
    _patch_layer(monkeypatch, decide_cfg=_decide_cfg("enforce"), decider=g4_error)
    _patch_adapters(monkeypatch)
    _patch_planner(monkeypatch)
    _patch_implementer(
        monkeypatch,
        claims=[{"text": CLAIM_TEXT, "doc_id": "doc-1"}],
        fields=FIVE_FIELDS,
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", DATA_EXIT_CONSENT)

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, with_llm=True)

    assert any(
        "decide layer degraded during run" in gap and "Jev errors" in gap
        for gap in result["gaps"]
    ), result["gaps"]
    assert result["decide"]["degraded"] is True
    dossier = json.loads(
        Path(result["paths"]["package_dir"]).joinpath("dossier.json").read_text(encoding="utf-8")
    )
    assert dossier["decide_degraded"] is True
    manifest = json.loads(
        Path(result["paths"]["package_dir"]).joinpath("manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["decide_degraded"] is True
    assert respx.calls.call_count == 0


@respx.mock
def test_shadow_jev_errors_recorded_but_never_flagged(tmp_path, monkeypatch):
    """S5: the same Jev errors in shadow ARE recorded as rows but the run is
    never flagged decide_degraded (rows yes, flag no — nothing bound)."""
    orch = FakeOrchLlm(docs=[_doc("doc-1", DOC_TEXT_1)])
    orch.config.storage.dossiers_dir = str(tmp_path / "dossiers")
    orch.snapshot = _snapshot()
    _patch_coverage(monkeypatch)
    g4_error = MockDecider(
        {
            "step": Decision(applies=True, ok=True, answers={"step": {"noul": 0.9}}, raw_tokens=10),
            "claim": Decision(applies=True, ok=False, answers={}, raw_tokens=0),
            "posture": Decision(
                applies=True, ok=True, answers={"posture": {"choice": "within_posture"}}, raw_tokens=5
            ),
            "routing": Decision(
                applies=True, ok=True, answers={"routing": {"choice": "quick"}}, raw_tokens=5
            ),
        }
    )
    _patch_layer(monkeypatch, decide_cfg=_decide_cfg("shadow"), decider=g4_error)
    _patch_adapters(monkeypatch)
    _patch_planner(monkeypatch)
    _patch_implementer(
        monkeypatch,
        claims=[{"text": CLAIM_TEXT, "doc_id": "doc-1"}],
        fields=FIVE_FIELDS,
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", DATA_EXIT_CONSENT)

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, with_llm=True)

    assert not any("decide layer degraded during run" in gap for gap in result["gaps"])
    assert result["decide"]["degraded"] is False
    dossier = json.loads(
        Path(result["paths"]["package_dir"]).joinpath("dossier.json").read_text(encoding="utf-8")
    )
    assert dossier["decide_degraded"] is False
    manifest = json.loads(
        Path(result["paths"]["package_dir"]).joinpath("manifest.json").read_text(encoding="utf-8")
    )
    assert "decide_degraded" not in manifest  # only written when truthy
    # the error rows ARE on the ledger
    rows = [
        json.loads(line)
        for line in Path(result["paths"]["decisions"]).read_text(encoding="utf-8").splitlines()
    ]
    assert any(row.get("error") for row in rows)
    assert respx.calls.call_count == 0


# ---------------------------------------------------------------------------
# 6c. Implement sub-stage robustness + budget
# ---------------------------------------------------------------------------


@respx.mock
def test_implement_pass_loads_bodies_through_raw_get(monkeypatch):
    """Q1: iter_docs is metadata-only — the pass must load each body through
    RawStore.get(); the fake store now enforces that contract."""
    orch = FakeOrchLlm(docs=[_doc("doc-1", DOC_TEXT_1)])
    _patch_coverage(monkeypatch)
    _patch_package_recorder(monkeypatch)
    _patch_layer(monkeypatch, decide_cfg=_decide_cfg("enforce"), decider=_high_decider())
    _patch_adapters(monkeypatch)
    impl_rec = _patch_implementer(
        monkeypatch,
        claims=[{"text": CLAIM_TEXT, "doc_id": "doc-1"}],
        fields=FIVE_FIELDS,
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", DATA_EXIT_CONSENT)

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, with_llm=True)

    # bodies came through get(): the doc text was seen, claims gated+accepted
    assert result["stages"]["implement"]["docs"] == 1
    assert result["stages"]["implement"]["claims_accepted"] == 1
    assert len(impl_rec["calls"]) >= 1
    assert not any("bodies loadable" in gap for gap in result["gaps"])


@respx.mock
def test_implement_pass_zero_loadable_bodies_records_gap(monkeypatch):
    """Q1: documents in scope but zero loadable bodies => a gap says so —
    never a silent no-op."""
    doc = Document(
        doc_id="doc-1",
        source="company_feed",
        url="https://acme.com/doc-1",
        domain=DOMAIN,
        fetched_at="2026-08-15T00:00:00+00:00",
        body=None,  # metadata row without a loadable body
    )
    orch = FakeOrchLlm(docs=[doc])
    _patch_coverage(monkeypatch)
    _patch_package_recorder(monkeypatch)
    _patch_layer(monkeypatch, decide_cfg=_decide_cfg("enforce"), decider=_high_decider())
    _patch_adapters(monkeypatch)
    impl_rec = _patch_implementer(monkeypatch, claims=[], fields={})
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", DATA_EXIT_CONSENT)

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, with_llm=True)

    assert any(
        "documents in scope but 0 bodies loadable" in gap for gap in result["gaps"]
    ), result["gaps"]
    assert result["stages"]["implement"]["docs"] == 0
    assert impl_rec["calls"] == []  # no implementer spend without bodies


@respx.mock
def test_five_fields_keep_their_own_names_when_a_middle_field_is_dropped(
    tmp_path, monkeypatch
):
    """Q2 regression: fields map by claim IDENTITY, not position — a
    prefilter-dropped middle field must not shift the survivors' names."""
    zero_overlap = "Quantum flux capacitor retro encabulator upgraded unprompted."
    fields = {
        "operational_need": {"text": CLAIM_TEXT, "doc_id": "doc-1"},
        "buying_window": {"text": zero_overlap, "doc_id": "doc-1"},
        "why_now": {"text": WHY_NOW_TEXT, "doc_id": "doc-1"},
    }
    orch = FakeOrchLlm(docs=[_doc("doc-1", DOC_TEXT_1)])
    orch.config.storage.dossiers_dir = str(tmp_path / "dossiers")
    orch.snapshot = _snapshot()
    _patch_coverage(monkeypatch)
    _patch_layer(monkeypatch, decide_cfg=_decide_cfg("enforce"), decider=_high_decider())
    _patch_adapters(monkeypatch)
    _patch_planner(monkeypatch)
    _patch_implementer(
        monkeypatch,
        claims=[{"text": CLAIM_TEXT, "doc_id": "doc-1"}],
        fields=fields,
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", DATA_EXIT_CONSENT)

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, with_llm=True)

    dossier = json.loads(
        Path(result["paths"]["package_dir"]).joinpath("dossier.json").read_text(encoding="utf-8")
    )
    llm_fields = dossier["llm_fields"]
    assert set(llm_fields) == {"operational_need", "why_now"}  # zero-overlap field dropped
    assert llm_fields["operational_need"]["text"] == CLAIM_TEXT
    assert llm_fields["why_now"]["text"] == WHY_NOW_TEXT
    assert respx.calls.call_count == 0


@respx.mock
def test_disabled_need_promotion_gate_never_attempts_promotion(tmp_path, monkeypatch):
    """Q3: need_promotion enabled:false => the gate is skipped entirely — no
    promotion attempt despite the high-noul mock, no need_promotion rows."""
    cfg = _decide_cfg("enforce")
    cfg["decider"]["gates"]["need_promotion"] = {"enabled": False, "floor": 0.70}
    orch = FakeOrchLlm(docs=[_doc("doc-1", DOC_TEXT_1)])
    orch.config.storage.dossiers_dir = str(tmp_path / "dossiers")
    orch.snapshot = _snapshot()
    _patch_coverage(monkeypatch)
    _patch_layer(monkeypatch, decide_cfg=cfg, decider=_high_decider())
    _patch_adapters(monkeypatch)
    _patch_planner(monkeypatch)
    _patch_implementer(
        monkeypatch,
        claims=[{"text": CLAIM_TEXT, "doc_id": "doc-1"}],
        fields={},
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", DATA_EXIT_CONSENT)

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, with_llm=True)

    # claims still flow through G4, but nothing is promoted
    assert result["stages"]["implement"]["claims_accepted"] == 1
    assert result["stages"]["implement"]["promoted"] == 0
    assert orch.signal_store.upserted == []
    rows = [
        json.loads(line)
        for line in Path(result["paths"]["decisions"]).read_text(encoding="utf-8").splitlines()
    ]
    assert rows and all(row["gate"] != "need_promotion" for row in rows)
    assert respx.calls.call_count == 0


@respx.mock
def test_g5_call_budget_cap_stops_promotion(monkeypatch):
    """Q4: 80 high-noul candidates (> MAX_G5_CALLS) => exactly MAX_G5_CALLS
    gated+promoted, the rest narrow-only, one cap gap, no crash."""
    claims_per_doc = 20
    docs = [_doc(f"doc-{n}", DOC_TEXT_1) for n in range(1, 5)]
    orch = FakeOrchLlm(docs=docs)
    _patch_coverage(monkeypatch)
    _patch_package_recorder(monkeypatch)
    claim_answers = {
        f"claim_{i}": {"noul": 0.9} for i in range(claims_per_doc)
    }
    decider = MockDecider(
        {
            "step": Decision(applies=True, ok=True, answers={"step": {"noul": 0.9}}, raw_tokens=10),
            "claim": Decision(applies=True, ok=True, answers=claim_answers, raw_tokens=40),
            "promotion": Decision(
                applies=True, ok=True, answers={"promotion": {"noul": 0.9}}, raw_tokens=10
            ),
        }
    )
    _patch_layer(monkeypatch, decide_cfg=_decide_cfg("enforce"), decider=decider)
    _patch_adapters(monkeypatch)
    _patch_planner(monkeypatch)
    rec: list = []

    def fake_call_implementer(messages, *, model, base_url, api_key, reasoning_effort=None, timeout_s=60.0):
        rec.append(model)
        system = messages[0]["content"]
        if "REASONING IMPLEMENTER" in system:
            return {}  # no five fields in this test
        user = messages[1]["content"]
        doc_id = user.split("Document doc_id: ", 1)[1].splitlines()[0].strip()
        return {
            "claims": [
                {
                    "text": (
                        "Acme is evaluating SIEM vendors after a breach last quarter "
                        f"option {i}"
                    ),
                    "doc_id": doc_id,
                }
                for i in range(claims_per_doc)
            ]
        }

    monkeypatch.setattr(intel.llm_implement, "call_implementer", fake_call_implementer)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", DATA_EXIT_CONSENT)

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, with_llm=True)

    total_candidates = 4 * claims_per_doc
    assert total_candidates > intel.MAX_G5_CALLS
    assert result["stages"]["implement"]["promoted"] == intel.MAX_G5_CALLS
    assert len(orch.signal_store.upserted) == intel.MAX_G5_CALLS
    assert any(
        f"G5 call budget cap reached ({intel.MAX_G5_CALLS})" in gap
        and "not promoted" in gap
        for gap in result["gaps"]
    ), result["gaps"]


@respx.mock
def test_g5_judges_cited_document_text_not_claim_echo(monkeypatch):
    """P3c: the G5 decider's state carries the CITED DOCUMENT's text
    (anchor-windowed around the claim), never just the claim's own echo."""
    orch = FakeOrchLlm(docs=[_doc("doc-1", DOC_TEXT_1)])
    _patch_coverage(monkeypatch)
    _patch_package_recorder(monkeypatch)
    decider = _RecordingDecider(_high_decider().verdicts)
    _patch_layer(monkeypatch, decide_cfg=_decide_cfg("enforce"), decider=decider)
    _patch_adapters(monkeypatch)
    _patch_planner(monkeypatch)
    _patch_implementer(
        monkeypatch,
        claims=[{"text": CLAIM_TEXT, "doc_id": "doc-1"}],
        fields={},
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", DATA_EXIT_CONSENT)

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, with_llm=True)

    assert result["stages"]["implement"]["promoted"] == 1
    assert len(decider.promotion_states) == 1
    state = decider.promotion_states[0]
    assert state["need"] == CLAIM_TEXT
    # document text beyond the claim itself reached the judge
    assert (
        "The platform team plans to consolidate three monitoring tools into one."
        in state["evidence_excerpt"]
    )
    assert respx.calls.call_count == 0


# ---------------------------------------------------------------------------
# 7. Dry run: skipped records, zero seam calls
# ---------------------------------------------------------------------------


def test_dry_run_with_layer_records_skips_and_never_calls_seams(monkeypatch):
    orch = FakeOrchLlm(docs=[_doc("doc-1", DOC_TEXT_1)])
    _patch_coverage(monkeypatch)
    _patch_package_recorder(monkeypatch)
    _patch_layer(monkeypatch, decide_cfg=_decide_cfg("enforce"), decider=_high_decider())
    planner_rec = _patch_planner(monkeypatch)
    impl_rec = _patch_implementer(monkeypatch, claims=[], fields={})

    result = intel.run_intel(
        DOMAIN, config=orch.config, orch=orch, with_llm=True, dry_run=True
    )

    assert result["stages"]["plan"] == {"status": "skipped", "note": "dry-run"}
    assert result["stages"]["implement"] == {"status": "skipped", "note": "dry-run"}
    assert planner_rec["calls"] == []
    assert impl_rec["calls"] == []
    assert result["decide"]["rows"] == 0
    assert result["decide"]["mode"] == "enforce"


# ---------------------------------------------------------------------------
# 8. The one-config-load helper
# ---------------------------------------------------------------------------


def test_load_decide_cfg_missing_file_is_empty(tmp_path, monkeypatch):
    cfg = Config()
    monkeypatch.setattr(cfg, "config_dir", str(tmp_path), raising=False)
    assert intel._load_decide_cfg(cfg) == {}


# ---------------------------------------------------------------------------
# 9. CLI: --with-llm threads through as with_llm
# ---------------------------------------------------------------------------


def _stub_run_intel(monkeypatch, calls):
    def fake_run_intel(*args, **kwargs):
        calls.append(kwargs)
        return {
            "domain": DOMAIN,
            "created": True,
            "stages": {name: {"status": "ran"} for name in intel_mod.STAGES},
            "errors": {},
            "coverage": [],
            "gaps": [],
            "dossier": None,
            "paths": {},
        }

    monkeypatch.setattr(cli_mod, "Orchestrator", lambda cfg: object())
    monkeypatch.setattr(intel_mod, "run_intel", fake_run_intel)


def test_cli_threads_with_llm_flag(monkeypatch):
    calls: list = []
    _stub_run_intel(monkeypatch, calls)
    runner = CliRunner()

    result = runner.invoke(cli_mod.main, ["intel", DOMAIN, "--with-llm"])
    assert result.exit_code == 0, result.output
    assert calls[0]["with_llm"] is True

    calls.clear()
    result = runner.invoke(cli_mod.main, ["intel", DOMAIN])
    assert result.exit_code == 0, result.output
    assert calls[0]["with_llm"] is False


# ---------------------------------------------------------------------------
# 10. Doctor: decide-layer posture checks (never echoes values)
# ---------------------------------------------------------------------------


class _CfgStub:
    def __init__(self, decide_cfg):
        self._decide = decide_cfg

    def load_yaml(self, name):
        if name == "decide":
            return self._decide
        raise FileNotFoundError(name)


def test_doctor_decide_lines_silent_when_off(monkeypatch):
    _clear_env(monkeypatch)
    lines = cli_mod._decide_doctor_lines(_CfgStub({"mode": "off"}))
    assert lines == []


def test_doctor_decide_lines_warn_without_consent(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    lines = cli_mod._decide_doctor_lines(_CfgStub({"mode": "enforce"}))
    assert len(lines) == 1
    status, name, detail = lines[0].split("\t")
    assert (status, name) == ("WARN", "decide_layer")
    assert "SIGNALS_DECIDE_DATA_EXIT" in detail
    assert DATA_EXIT_CONSENT not in detail  # never echoes the value


def test_doctor_decide_lines_warn_on_missing_keys(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", DATA_EXIT_CONSENT)
    lines = cli_mod._decide_doctor_lines(_CfgStub({"mode": "shadow"}))
    assert len(lines) == 1
    status, name, detail = lines[0].split("\t")
    assert (status, name) == ("WARN", "decide_layer_keys")
    assert "TYPESAFE_API_KEY" in detail and "OPENROUTER_API_KEY" in detail


def test_doctor_decide_lines_silent_when_fully_configured(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", DATA_EXIT_CONSENT)
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    lines = cli_mod._decide_doctor_lines(_CfgStub({"mode": "enforce"}))
    assert lines == []
