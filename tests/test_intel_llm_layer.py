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


# ---------------------------------------------------------------------------
# 11. Document gate: staged docs screened BEFORE implementer spend
# ---------------------------------------------------------------------------


DOC_GATE_CFG = {
    "enabled": True,
    "relevant_min": 0.45,
    "evidence_min": 0.55,
    "injection_max": 0.70,
    "on_error": "use_deterministic",
}


class _DocGateSelectiveDecider:
    """Doc-gate-aware decider: excludes docs named in ``exclude`` (reason
    irrelevant: the relevant noul drops below relevant_min); delegates every
    other question to an inner decider."""

    def __init__(self, exclude, inner) -> None:
        self.exclude = set(exclude)
        self.inner = inner

    def decide(self, state, questions):
        if "is_relevant" in questions:
            relevant = 0.1 if state.get("doc_id") in self.exclude else 0.9
            return Decision(
                applies=True,
                ok=True,
                answers={
                    "is_relevant": {"noul": relevant},
                    "contains_signal_evidence": {"noul": 0.9},
                    "contains_prompt_injection": {"noul": 0.0},
                },
                raw_tokens=5,
            )
        return self.inner.decide(state, questions)


def _patch_doc_recorder(monkeypatch, *, fields=None):
    """Implementer recorder that captures WHICH doc each call extracts from."""
    rec = {"bulk": [], "fields": []}

    def fake_call_implementer(
        messages, *, model, base_url, api_key, reasoning_effort=None, timeout_s=60.0
    ):
        doc_id = messages[1]["content"].split("Document doc_id: ", 1)[1].splitlines()[0].strip()
        if "REASONING IMPLEMENTER" in messages[0]["content"]:
            rec["fields"].append(doc_id)
            return fields or {}
        rec["bulk"].append(doc_id)
        return {"claims": [{"text": CLAIM_TEXT, "doc_id": doc_id}]}

    monkeypatch.setattr(intel.llm_implement, "call_implementer", fake_call_implementer)
    return rec


def _doc_gate_orch(docs):
    orch = FakeOrchLlm(docs=docs)
    orch.snapshot = _snapshot()
    return orch


@respx.mock
def test_document_gate_excluded_doc_never_reaches_implementer(tmp_path, monkeypatch):
    """Enforce: the excluded doc is screened out BEFORE doc_specs — the bulk
    implementer is never spent on it."""
    cfg = _decide_cfg("enforce")
    cfg["decider"]["gates"]["document_gate"] = DOC_GATE_CFG
    orch = _doc_gate_orch([_doc("doc-1", DOC_TEXT_1), _doc("doc-2", DOC_TEXT_1)])
    orch.config.storage.dossiers_dir = str(tmp_path / "dossiers")
    _patch_coverage(monkeypatch)
    _patch_layer(
        monkeypatch,
        decide_cfg=cfg,
        decider=_DocGateSelectiveDecider({"doc-1"}, _high_decider()),
    )
    _patch_adapters(monkeypatch)
    _patch_planner(monkeypatch)
    rec = _patch_doc_recorder(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", DATA_EXIT_CONSENT)

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, with_llm=True)

    stage = result["stages"]["implement"]
    assert stage["status"] == "ran"
    assert stage["docs_screened"] == 2
    assert stage["docs_excluded"] == 1
    assert stage["docs_excluded_reasons"] == {"irrelevant": 1}
    assert rec["bulk"] == ["doc-2"]  # doc-1 never reached the implementer
    assert stage["claims_accepted"] == 1

    rows = [
        json.loads(line)
        for line in Path(result["paths"]["decisions"]).read_text(encoding="utf-8").splitlines()
    ]
    doc_rows = [r for r in rows if r["gate"] == "document_gate"]
    assert len(doc_rows) == 2  # one row per screened doc
    assert [r["outcome"] for r in doc_rows] == ["excluded", "included"]
    assert doc_rows[0]["reason"] == "irrelevant"
    assert respx.calls.call_count == 0


@respx.mock
def test_document_gate_all_excluded_records_gap_and_skips_implementer(
    tmp_path, monkeypatch
):
    """Enforce + every staged doc excluded => the gap says so, the implementer
    loop never runs, and the deterministic run completes unharmed."""
    cfg = _decide_cfg("enforce")
    cfg["decider"]["gates"]["document_gate"] = DOC_GATE_CFG
    orch = _doc_gate_orch([_doc("doc-1", DOC_TEXT_1), _doc("doc-2", DOC_TEXT_1)])
    orch.config.storage.dossiers_dir = str(tmp_path / "dossiers")
    _patch_coverage(monkeypatch)
    _patch_layer(
        monkeypatch,
        decide_cfg=cfg,
        decider=_DocGateSelectiveDecider({"doc-1", "doc-2"}, _high_decider()),
    )
    _patch_adapters(monkeypatch)
    _patch_planner(monkeypatch)
    rec = _patch_doc_recorder(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", DATA_EXIT_CONSENT)

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, with_llm=True)

    assert any(
        "document gate excluded all 2 staged documents; implement pass no-op"
        in gap
        for gap in result["gaps"]
    ), result["gaps"]
    assert rec["bulk"] == [] and rec["fields"] == []  # no implementer spend at all
    stage = result["stages"]["implement"]
    assert stage["status"] == "ran"  # the run completes
    assert stage["docs_excluded"] == 2
    assert stage["claims_accepted"] == 0
    assert orch.signal_store.upserted == []
    rows = [
        json.loads(line)
        for line in Path(result["paths"]["decisions"]).read_text(encoding="utf-8").splitlines()
    ]
    assert [r["outcome"] for r in rows if r["gate"] == "document_gate"] == [
        "excluded",
        "excluded",
    ]
    assert respx.calls.call_count == 0


@respx.mock
def test_document_gate_term_counts_in_preflight_degradation(tmp_path, monkeypatch):
    """The pre-flight models the doc gate's per-doc request BEFORE the first
    Jev call: two ~700-token docs over a 21000-token ceiling degrade the whole
    run to shadow ONLY because of the doc-gate term (G4 term + allowances fit)."""
    body = "Acme breach quarter evaluating vendors. " * 70  # 2800 chars ~ 700 tokens
    cfg = _decide_cfg("enforce", max_decide_tokens_per_run=21000)
    cfg["decider"]["gates"]["document_gate"] = DOC_GATE_CFG
    orch = _doc_gate_orch([_doc("doc-1", body), _doc("doc-2", body)])
    orch.config.storage.dossiers_dir = str(tmp_path / "dossiers")
    _patch_coverage(monkeypatch)
    _patch_layer(
        monkeypatch,
        decide_cfg=cfg,
        decider=_DocGateSelectiveDecider({"doc-1"}, _high_decider()),
    )
    _patch_adapters(monkeypatch)
    _patch_planner(monkeypatch)
    rec = _patch_doc_recorder(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", DATA_EXIT_CONSENT)

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, with_llm=True)

    # G4 term (2x700) + doc-gate term (2x700) + allowances (700+700+60*300):
    # the gap line carries the exact projection vs the ceiling.
    assert any(
        "decide token budget exceeded (22200 > 21000)" in gap for gap in result["gaps"]
    ), result["gaps"]
    assert any("decide token budget exceeded" in gap for gap in result["gaps"])
    assert result["decide"]["mode"] == "shadow"  # whole-run degradation
    assert result["decide"]["degraded"] is True
    # Degraded to shadow: the gate screened every doc but excluded NOTHING.
    assert result["stages"]["implement"]["docs_excluded"] == 0
    assert sorted(rec["bulk"]) == ["doc-1", "doc-2"]
    assert respx.calls.call_count == 0


@respx.mock
def test_document_gate_shadow_screens_but_excludes_nothing(tmp_path, monkeypatch):
    """Shadow: every staged doc is screened (rows recorded with the would-be
    exclusion) but none is dropped — shadow never binds."""
    cfg = _decide_cfg("shadow")
    cfg["decider"]["gates"]["document_gate"] = DOC_GATE_CFG
    orch = _doc_gate_orch([_doc("doc-1", DOC_TEXT_1), _doc("doc-2", DOC_TEXT_1)])
    orch.config.storage.dossiers_dir = str(tmp_path / "dossiers")
    _patch_coverage(monkeypatch)
    _patch_layer(
        monkeypatch,
        decide_cfg=cfg,
        decider=_DocGateSelectiveDecider({"doc-1"}, _high_decider()),
    )
    _patch_adapters(monkeypatch)
    _patch_planner(monkeypatch)
    rec = _patch_doc_recorder(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", DATA_EXIT_CONSENT)

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, with_llm=True)

    stage = result["stages"]["implement"]
    assert stage["docs_screened"] == 2
    assert stage["docs_excluded"] == 0  # shadow never binds
    assert sorted(rec["bulk"]) == ["doc-1", "doc-2"]  # both still implemented
    rows = [
        json.loads(line)
        for line in Path(result["paths"]["decisions"]).read_text(encoding="utf-8").splitlines()
    ]
    doc_rows = [r for r in rows if r["gate"] == "document_gate"]
    assert [r["outcome"] for r in doc_rows] == ["excluded", "included"]  # would-be
    assert respx.calls.call_count == 0


# ---------------------------------------------------------------------------
# 12. Output screen wiring: screen_cfg reaches BOTH G4 call sites
# ---------------------------------------------------------------------------


SCREEN_CFG = {
    "enabled": True,
    "review_threshold": 0.35,
    "action_threshold": 0.70,
    "on_error": "use_deterministic",
}


class _ScreenDecider(MockDecider):
    """Decider whose batch answers carry a high wrong_entity noul on top of
    per-claim Choice verdicts (the screen's batch-rides-the-request shape)."""

    def __init__(self) -> None:
        batch = Decision(
            applies=True,
            ok=True,
            answers={
                "claim_0": {"choice": "supports", "probabilities": {"supports": 0.95}},
                "out_of_excerpt": {"noul": 0.0},
                "wrong_entity": {"noul": 0.9},
            },
            raw_tokens=40,
        )
        super().__init__(
            {
                "step": Decision(
                    applies=True, ok=True, answers={"step": {"noul": 0.9}}, raw_tokens=10
                ),
                "claim": batch,
                "posture": Decision(
                    applies=True,
                    ok=True,
                    answers={"posture": {"choice": "within_posture"}},
                    raw_tokens=5,
                ),
                "routing": Decision(
                    applies=True, ok=True, answers={"routing": {"choice": "quick"}}, raw_tokens=5
                ),
                "promotion": Decision(
                    applies=True, ok=True, answers={"promotion": {"noul": 0.9}}, raw_tokens=10
                ),
            }
        )


@respx.mock
def test_output_screen_wiring_blocks_batch_on_wrong_entity(tmp_path, monkeypatch):
    """Intel-level pin of the output_screen threading (intel.py resolves
    ``screen_cfg = _gate_cfg(decide_cfg, "output_screen")`` and threads it
    into BOTH screen_and_gate_claims call sites): with the screen enabled and
    a wrong_entity noul >= action_threshold, every claim batch is blocked
    (claims_accepted == 0) and the ledger carries reason "wrong_entity".
    Dropping the screen_cfg kwarg would silently disable the screen while the
    gate-level tests stay green — this test fails if either call site loses
    the kwarg."""
    cfg = _decide_cfg("enforce")
    cfg["decider"]["gates"]["output_screen"] = SCREEN_CFG
    orch = FakeOrchLlm(docs=[_doc("doc-1", DOC_TEXT_1)])
    orch.config.storage.dossiers_dir = str(tmp_path / "dossiers")
    orch.snapshot = _snapshot()
    _patch_coverage(monkeypatch)
    _patch_layer(monkeypatch, decide_cfg=cfg, decider=_ScreenDecider())
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

    # The block binds in enforce: zero accepted claims, zero promotions.
    assert result["stages"]["implement"]["claims_accepted"] == 0
    assert orch.signal_store.upserted == []
    rows = [
        json.loads(line)
        for line in Path(result["paths"]["decisions"]).read_text(encoding="utf-8").splitlines()
    ]
    citation_rows = [r for r in rows if r["gate"] == "citation_soundness"]
    assert citation_rows  # the claims were judged, then blocked
    assert any(r.get("reason") == "wrong_entity" for r in citation_rows)
    assert respx.calls.call_count == 0


# ---------------------------------------------------------------------------
# 13. Completeness-verify cascade wiring (wave-2 task 2)
# ---------------------------------------------------------------------------
#
# The battery runs on the five-fields result of the LONGEST doc, AFTER the
# existing G4 screening (extract -> screen -> llm_fields). Fired PRESENT
# fields are QUARANTINED (dropped from llm_fields); fired UNKNOWN fields
# ESCALATE: one reasoning-model re-run of extract_five_fields, re-screened
# through the same G4 path, replacing the fired unknown fields it fills
# (meta gains "escalated": True), then RE-VERIFIED (the loop's one-request-
# per-escalation pre-flight term) until quiet or the max_escalations cap.


COMP_GATE_CFG = {
    "enabled": True,
    "fire_threshold": 0.70,
    "max_escalations": 20,
    "on_error": "skip",
}


def _comp_cfg(**gate_overrides):
    """A decide cfg WITH the completeness_verify block (the wave-2 opt-in:
    cfgs without the block keep today's behavior exactly)."""
    cfg = _decide_cfg("enforce")
    block = dict(COMP_GATE_CFG)
    block.update(gate_overrides)
    cfg["decider"]["gates"]["completeness_verify"] = block
    return cfg


#: why_now comes back unknown: the escalation trigger.
FIELDS_V1 = {
    "operational_need": {"text": CLAIM_TEXT, "doc_id": "doc-1"},
    "why_now": {"text": "unknown", "doc_id": "doc-1"},
}

#: The escalation re-run fills why_now.
FIELDS_V2 = {
    "operational_need": {"text": CLAIM_TEXT, "doc_id": "doc-1"},
    "why_now": {"text": WHY_NOW_TEXT, "doc_id": "doc-1"},
}


class _CompletenessDecider:
    """Answers the completeness battery per script (head ids carry the
    ``<field>::absence_wrong`` / ``<field>::grounded`` suffixes); every other
    question goes to ``inner``. Scripted heads get their noul, unscripted
    heads a quiet 0.1."""

    def __init__(self, values, inner) -> None:
        self.values = dict(values)
        self.inner = inner

    def decide(self, state, questions):
        if any("::absence_wrong" in q or "::grounded" in q for q in questions):
            answers = {q: {"noul": self.values.get(q, 0.1)} for q in questions}
            return Decision(applies=True, ok=True, answers=answers, raw_tokens=30)
        return self.inner.decide(state, questions)


def _patch_field_sequence(monkeypatch, *, fields_sequence):
    """Implementer recorder returning fields_sequence[i] on the i-th
    REASONING call (clamped to the last) and one bulk claim per doc."""
    rec = {"bulk": [], "fields": []}

    def fake_call_implementer(
        messages, *, model, base_url, api_key, reasoning_effort=None, timeout_s=60.0
    ):
        doc_id = messages[1]["content"].split("Document doc_id: ", 1)[1].splitlines()[0].strip()
        if "REASONING IMPLEMENTER" in messages[0]["content"]:
            rec["fields"].append(doc_id)
            return fields_sequence[min(len(rec["fields"]) - 1, len(fields_sequence) - 1)]
        rec["bulk"].append(doc_id)
        return {"claims": [{"text": CLAIM_TEXT, "doc_id": doc_id}]}

    monkeypatch.setattr(intel.llm_implement, "call_implementer", fake_call_implementer)
    return rec


def _comp_run(tmp_path, monkeypatch, *, cfg, decider, fields_sequence, body=DOC_TEXT_1):
    """One full run with the completeness cascade configured; returns the
    result, the written dossier and the implementer recorder."""
    orch = FakeOrchLlm(docs=[_doc("doc-1", body)])
    orch.config.storage.dossiers_dir = str(tmp_path / "dossiers")
    orch.snapshot = _snapshot()
    _patch_coverage(monkeypatch)
    _patch_layer(monkeypatch, decide_cfg=cfg, decider=decider)
    _patch_adapters(monkeypatch)
    _patch_planner(monkeypatch)
    rec = _patch_field_sequence(monkeypatch, fields_sequence=fields_sequence)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", DATA_EXIT_CONSENT)
    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, with_llm=True)
    dossier = json.loads(
        Path(result["paths"]["package_dir"]).joinpath("dossier.json").read_text(encoding="utf-8")
    )
    return result, dossier, rec


@respx.mock
def test_completeness_escalation_reruns_reasoning_model_and_marks_fields(
    tmp_path, monkeypatch
):
    """An unknown field whose absence_wrong head fires: extract_five_fields
    runs TWICE for the doc, the re-run's fill replaces the unknown with
    ``escalated: True`` meta, and the stage record counts the escalation."""
    decider = _CompletenessDecider({"why_now::absence_wrong": 0.95}, _high_decider())
    result, dossier, rec = _comp_run(
        tmp_path, monkeypatch, cfg=_comp_cfg(), decider=decider,
        fields_sequence=[FIELDS_V1, FIELDS_V2],
    )

    assert rec["fields"] == ["doc-1", "doc-1"]  # extract called TWICE for the doc
    llm = dossier["llm_fields"]
    assert set(llm) == {"operational_need", "why_now"}
    assert llm["why_now"]["text"] == WHY_NOW_TEXT
    assert llm["why_now"]["escalated"] is True
    assert "escalated" not in llm["operational_need"]  # only the re-run is marked
    assert result["stages"]["implement"]["five_fields_escalations"] == 1

    rows = [
        json.loads(line)
        for line in Path(result["paths"]["decisions"]).read_text(encoding="utf-8").splitlines()
    ]
    comp_rows = [r for r in rows if r["gate"] == "completeness_verify"]
    # initial verification fired; the RE-VERIFICATION of the re-run passed
    assert [r["outcome"] for r in comp_rows] == ["escalated", "passed"]
    assert respx.calls.call_count == 0


@respx.mock
def test_completeness_quarantine_drops_hallucinated_field(tmp_path, monkeypatch):
    """A present field whose grounded head fires: it is REMOVED from
    llm_fields (a hallucinated fill must not survive) and nothing escalates."""
    decider = _CompletenessDecider({"operational_need::grounded": 0.95}, _high_decider())
    result, dossier, rec = _comp_run(
        tmp_path, monkeypatch, cfg=_comp_cfg(), decider=decider,
        fields_sequence=[FIVE_FIELDS],
    )

    assert rec["fields"] == ["doc-1"]  # no escalation: the fired field was PRESENT
    assert set(dossier["llm_fields"]) == {"why_now"}  # operational_need quarantined
    stage = result["stages"]["implement"]
    assert "five_fields_escalations" not in stage
    assert stage["five_fields_quarantined"] == 1
    assert respx.calls.call_count == 0


@respx.mock
def test_completeness_cap_blocks_second_escalation_and_records_gap(
    tmp_path, monkeypatch
):
    """max_escalations 1 with two escalating versions of the doc (the re-run
    comes back unknown too): the first escalation happens, the second is
    blocked, the gap line fires, extract runs exactly twice."""
    cfg = _comp_cfg(max_escalations=1)
    decider = _CompletenessDecider({"why_now::absence_wrong": 0.95}, _high_decider())
    result, dossier, rec = _comp_run(
        tmp_path, monkeypatch, cfg=cfg, decider=decider,
        fields_sequence=[FIELDS_V1, FIELDS_V1],  # the re-run is unknown too
    )

    assert rec["fields"] == ["doc-1", "doc-1"]  # one escalation re-run, no more
    assert "why_now" not in dossier["llm_fields"]  # never filled
    assert result["stages"]["implement"]["five_fields_escalations"] == 1
    assert any(
        "completeness escalation cap reached (1); remaining docs unverified" in gap
        for gap in result["gaps"]
    ), result["gaps"]
    assert respx.calls.call_count == 0


@respx.mock
def test_completeness_disabled_gate_leaves_the_pass_unchanged(tmp_path, monkeypatch):
    """enabled: false (or the block absent) => zero behavioral delta: one
    extract call, no cascade, no completeness_verify rows."""
    cfg = _comp_cfg(enabled=False)
    decider = _CompletenessDecider({"why_now::absence_wrong": 0.95}, _high_decider())
    result, dossier, rec = _comp_run(
        tmp_path, monkeypatch, cfg=cfg, decider=decider,
        fields_sequence=[FIELDS_V1],
    )

    assert rec["fields"] == ["doc-1"]  # extract once — no escalation re-run
    assert set(dossier["llm_fields"]) == {"operational_need"}
    stage = result["stages"]["implement"]
    assert "five_fields_escalations" not in stage
    rows = [
        json.loads(line)
        for line in Path(result["paths"]["decisions"]).read_text(encoding="utf-8").splitlines()
    ]
    assert all(row["gate"] != "completeness_verify" for row in rows)
    assert respx.calls.call_count == 0


@respx.mock
def test_completeness_layer_off_config_present_does_nothing(tmp_path, monkeypatch):
    """The completeness block in decide.yaml alone does nothing: without
    --with-llm the run stays byte-identical (no extract, no rows)."""
    orch = FakeOrchLlm(docs=[_doc("doc-1", DOC_TEXT_1)])
    orch.config.storage.dossiers_dir = str(tmp_path / "dossiers")
    _patch_coverage(monkeypatch)
    _patch_package_recorder(monkeypatch)
    _patch_layer(monkeypatch, decide_cfg=_comp_cfg(), decider=_high_decider())
    _patch_planner(monkeypatch)
    rec = _patch_field_sequence(monkeypatch, fields_sequence=[FIELDS_V1])

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch)  # no with_llm

    assert "decide" not in result
    assert rec["fields"] == [] and rec["bulk"] == []
    assert not any("completeness" in gap for gap in result["gaps"])
    assert respx.calls.call_count == 0


@respx.mock
def test_completeness_preflight_term_degrades_run_to_shadow(tmp_path, monkeypatch):
    """The pre-flight models the cascade BEFORE the first Jev call: one
    ~700-token verify request per five-fields doc + one per potential
    escalation. Without the block the same run stays under the ceiling."""
    body = "Acme breach quarter evaluating vendors. " * 70  # 2800 chars ~ 700 tokens
    cfg = _comp_cfg()
    cfg["decider"]["max_decide_tokens_per_run"] = 22000
    decider = _CompletenessDecider({"why_now::absence_wrong": 0.95}, _high_decider())
    result, _dossier, rec = _comp_run(
        tmp_path, monkeypatch, cfg=cfg, decider=decider, fields_sequence=[FIELDS_V1],
        body=body,
    )

    # G4 (700) + doc gate (700) + completeness (2x700) + allowances (19400):
    # the degradation is caused by the completeness term alone (20800 fits).
    assert any(
        "decide token budget exceeded (22200 > 22000)" in gap for gap in result["gaps"]
    ), result["gaps"]
    assert result["decide"]["mode"] == "shadow"
    assert result["decide"]["degraded"] is True
    # Degraded to shadow the battery records but binds NOTHING: no escalation.
    assert rec["fields"] == ["doc-1"]

    # The same setup WITHOUT the completeness block stays under the ceiling:
    # the term is opt-in (cfgs without the block keep today's projection).
    orch = FakeOrchLlm(docs=[_doc("doc-1", body)])
    orch.config.storage.dossiers_dir = str(tmp_path / "dossiers2")
    orch.snapshot = _snapshot()
    _patch_layer(
        monkeypatch,
        decide_cfg=_decide_cfg("enforce", max_decide_tokens_per_run=22000),
        decider=decider,
    )
    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, with_llm=True)
    assert result["decide"]["mode"] == "enforce"
    assert result["decide"]["degraded"] is False
    assert not any("token budget" in gap for gap in result["gaps"])
    assert respx.calls.call_count == 0


# ---------------------------------------------------------------------------
# 13. Taxonomy typing wiring: classify_claims rides _implement_pass after G4,
#     proposals thread into claims_to_candidates -> evidence_data (enforce
#     only; the deterministic signal_type NEVER changes)
# ---------------------------------------------------------------------------


PLATFORM_TEXT = "The platform team plans to consolidate three monitoring tools into one."

#: Mirrors config/decide.yaml's wave-2 block (the typing battery's opt-in).
TYPING_GATE_CFG = {
    "enabled": True,
    "confident": 0.90,
    "ambiguous": 0.60,
    "on_error": "skip",
}


def _typing_cfg(mode="enforce", **decider_overrides):
    """A decide cfg WITH the taxonomy_typing block (the wave-2 opt-in: cfgs
    without the block keep today's behavior exactly)."""
    cfg = _decide_cfg(mode, **decider_overrides)
    cfg["decider"]["gates"]["taxonomy_typing"] = dict(TYPING_GATE_CFG)
    return cfg


class _TypingDecider(MockDecider):
    """_high_decider plus scripted answers for the typing battery (one
    Decision under the ``type`` prefix serves every type_<i> question) and a
    record of every request for wiring assertions."""

    def __init__(self, type_answers: dict[str, dict]) -> None:
        verdicts = dict(_high_decider().verdicts)
        verdicts["type"] = Decision(
            applies=True, ok=True, answers=dict(type_answers), raw_tokens=40
        )
        super().__init__(verdicts)
        self.requests: list[tuple[object, dict]] = []

    def decide(self, state, questions):
        self.requests.append((state, dict(questions)))
        return super().decide(state, questions)


@respx.mock
def test_taxonomy_typing_proposals_land_in_evidence_data(tmp_path, monkeypatch):
    """Enforce: one typing request per claim-bearing doc; each proposal rides
    into its candidate's evidence_data through the REAL normalize path while
    signal_type stays the deterministic llm_need."""
    orch = FakeOrchLlm(docs=[_doc("doc-1", DOC_TEXT_1)])
    orch.config.storage.dossiers_dir = str(tmp_path / "dossiers")
    orch.snapshot = _snapshot()
    _patch_coverage(monkeypatch)
    decider = _TypingDecider(
        {
            "type_0": {"choice": "llm_need", "probabilities": {"llm_need": 0.95, "award": 0.30}},
            "type_1": {
                "choice": "funding_round",
                "probabilities": {"funding_round": 0.72, "llm_need": 0.20},
            },
        }
    )
    _patch_layer(monkeypatch, decide_cfg=_typing_cfg(), decider=decider)
    _patch_adapters(monkeypatch)
    _patch_planner(monkeypatch)
    _patch_implementer(
        monkeypatch,
        claims=[
            {"text": CLAIM_TEXT, "doc_id": "doc-1"},
            {"text": PLATFORM_TEXT, "doc_id": "doc-1"},
        ],
        fields={},
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", DATA_EXIT_CONSENT)

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, with_llm=True)

    stage = result["stages"]["implement"]
    assert stage["status"] == "ran"
    assert stage["claims_accepted"] == 2
    assert stage["claims_typed"] == 2  # both accepted claims got a proposal

    # exactly ONE typing request, over BOTH accepted claims of the one doc
    typing_requests = [
        (state, questions)
        for state, questions in decider.requests
        if questions and all(qid.startswith("type_") for qid in questions)
    ]
    assert len(typing_requests) == 1
    _state, questions = typing_requests[0]
    assert sorted(questions) == ["type_0", "type_1"]
    assert len(questions["type_0"]["criteria"]) == 54  # the REAL taxonomy's options

    assert len(orch.signal_store.upserted) == 2
    sig0, sig1 = orch.signal_store.upserted
    # claim 0: confident AND well separated -> the TYPE proposal lands
    assert sig0.signal_type == "llm_need"  # the deterministic pipeline is authoritative
    assert sig0.evidence_data["proposed_type"] == "llm_need"
    assert sig0.evidence_data["type_confidence"] == 0.95
    assert sig0.evidence_data["type_separation"] == 0.95 / 0.30
    assert "proposed_category" not in sig0.evidence_data
    # claim 1: 0.72 is ambiguous-band -> the CATEGORY proposal lands
    assert sig1.signal_type == "llm_need"
    assert sig1.evidence_data["proposed_category"] == "financial"
    assert sig1.evidence_data["type_confidence"] == 0.72
    assert sig1.evidence_data["type_separation"] == 0.72 / 0.20
    assert "proposed_type" not in sig1.evidence_data

    rows = [
        json.loads(line)
        for line in Path(result["paths"]["decisions"]).read_text(encoding="utf-8").splitlines()
    ]
    typing_rows = [r for r in rows if r["gate"] == "taxonomy_typing"]
    assert [r["outcome"] for r in typing_rows] == ["proposed_type", "proposed_category"]
    assert respx.calls.call_count == 0


@respx.mock
def test_taxonomy_typing_disabled_gate_never_requests(tmp_path, monkeypatch):
    """The typing block absent (every pre-wave-2 config): NO typing request
    is made, no proposals land, the stage record gains no claims_typed."""
    orch = FakeOrchLlm(docs=[_doc("doc-1", DOC_TEXT_1)])
    orch.config.storage.dossiers_dir = str(tmp_path / "dossiers")
    orch.snapshot = _snapshot()
    _patch_coverage(monkeypatch)
    decider = _TypingDecider(
        {"type_0": {"choice": "llm_need", "probabilities": {"llm_need": 0.95, "award": 0.30}}}
    )
    _patch_layer(monkeypatch, decide_cfg=_decide_cfg("enforce"), decider=decider)
    _patch_adapters(monkeypatch)
    _patch_planner(monkeypatch)
    _patch_implementer(
        monkeypatch, claims=[{"text": CLAIM_TEXT, "doc_id": "doc-1"}], fields={}
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", DATA_EXIT_CONSENT)

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, with_llm=True)

    assert result["stages"]["implement"]["claims_accepted"] == 1
    assert "claims_typed" not in result["stages"]["implement"]
    assert not any(
        qid.startswith("type_") for _state, questions in decider.requests for qid in questions
    )
    assert orch.signal_store.upserted
    evidence = orch.signal_store.upserted[0].evidence_data
    assert "proposed_type" not in evidence
    assert "proposed_category" not in evidence
    assert respx.calls.call_count == 0


@respx.mock
def test_taxonomy_typing_shadow_records_rows_but_no_proposals(tmp_path, monkeypatch):
    """Shadow: the typing battery runs (rows record what enforce WOULD do)
    but returns NO proposals — enforce-only enrichment, a clean A/B."""
    orch = FakeOrchLlm(docs=[_doc("doc-1", DOC_TEXT_1)])
    orch.config.storage.dossiers_dir = str(tmp_path / "dossiers")
    orch.snapshot = _snapshot()
    _patch_coverage(monkeypatch)
    decider = _TypingDecider(
        {"type_0": {"choice": "llm_need", "probabilities": {"llm_need": 0.95, "award": 0.30}}}
    )
    _patch_layer(monkeypatch, decide_cfg=_typing_cfg("shadow"), decider=decider)
    _patch_adapters(monkeypatch)
    _patch_planner(monkeypatch)
    _patch_implementer(
        monkeypatch, claims=[{"text": CLAIM_TEXT, "doc_id": "doc-1"}], fields={}
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", DATA_EXIT_CONSENT)

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, with_llm=True)

    assert result["decide"]["mode"] == "shadow"
    assert "claims_typed" not in result["stages"]["implement"]  # nothing bound
    rows = [
        json.loads(line)
        for line in Path(result["paths"]["decisions"]).read_text(encoding="utf-8").splitlines()
    ]
    typing_rows = [r for r in rows if r["gate"] == "taxonomy_typing"]
    assert [r["outcome"] for r in typing_rows] == ["proposed_type"]  # would-be
    assert orch.signal_store.upserted == []  # shadow never promotes either
    assert respx.calls.call_count == 0


@respx.mock
def test_taxonomy_typing_preflight_term_degrades_run_to_shadow(tmp_path, monkeypatch):
    """The pre-flight models typing BEFORE the first Jev call: a flat
    ~800-token allowance per staged doc (acceptance is only known after G4,
    so EVERY staged doc is counted — the documented honest upper bound).
    Two ~700-token docs over a 23000-token ceiling degrade ONLY because of
    the typing term; without the block the same run stays enforce."""
    body = "Acme breach quarter evaluating vendors. " * 70  # 2800 chars ~ 700 tokens
    decider = _TypingDecider(
        {"type_0": {"choice": "llm_need", "probabilities": {"llm_need": 0.95, "award": 0.30}}}
    )
    orch = FakeOrchLlm(docs=[_doc("doc-1", body), _doc("doc-2", body)])
    orch.config.storage.dossiers_dir = str(tmp_path / "dossiers")
    orch.snapshot = _snapshot()
    _patch_coverage(monkeypatch)
    _patch_layer(
        monkeypatch,
        decide_cfg=_typing_cfg("enforce", max_decide_tokens_per_run=23000),
        decider=decider,
    )
    _patch_adapters(monkeypatch)
    _patch_planner(monkeypatch)
    _patch_implementer(
        monkeypatch, claims=[{"text": CLAIM_TEXT, "doc_id": "doc-1"}], fields={}
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", DATA_EXIT_CONSENT)

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, with_llm=True)

    # G4 (2x700) + doc gate (2x700) + typing (2x800) + allowances (19400):
    # the typing term alone pushes the projection over the ceiling.
    assert any(
        "decide token budget exceeded (23800 > 23000)" in gap for gap in result["gaps"]
    ), result["gaps"]
    assert result["decide"]["mode"] == "shadow"
    assert result["decide"]["degraded"] is True
    assert "claims_typed" not in result["stages"]["implement"]  # degraded: no proposals

    # The SAME run without the typing block stays under the ceiling: the term
    # is opt-in (cfgs without the block keep today's projection).
    orch = FakeOrchLlm(docs=[_doc("doc-1", body), _doc("doc-2", body)])
    orch.config.storage.dossiers_dir = str(tmp_path / "dossiers2")
    orch.snapshot = _snapshot()
    _patch_layer(
        monkeypatch,
        decide_cfg=_decide_cfg("enforce", max_decide_tokens_per_run=23000),
        decider=decider,
    )
    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, with_llm=True)
    assert result["decide"]["mode"] == "enforce"
    assert result["decide"]["degraded"] is False
    assert not any("token budget" in gap for gap in result["gaps"])
    assert respx.calls.call_count == 0


# ---------------------------------------------------------------------------
# 14. Event-date wiring: extract_event_date rides _implement_pass for docs
#     with >= 1 accepted claim; event_at threads into evidence_data while
#     observed_at STAYS the document's fetched_at (the map's carrier
#     decision — no schema change, no decay change)
# ---------------------------------------------------------------------------


#: Mirrors config/decide.yaml's wave-2 block (the date battery's opt-in).
DATES_GATE_CFG = {"enabled": True, "review_below": 0.60, "on_error": "skip"}

DATE_QIDS = ["mode", "month", "day", "year", "day_anchor", "weekday", "week_offset"]


def _dates_cfg(mode="enforce", **decider_overrides):
    """A decide cfg WITH the event_dates block (the wave-2 opt-in: cfgs
    without the block keep today's behavior exactly)."""
    cfg = _decide_cfg(mode, **decider_overrides)
    cfg["decider"]["gates"]["event_dates"] = dict(DATES_GATE_CFG)
    return cfg


def _dates_answers(
    mode="absolute",
    month="8",
    day="15",
    year="2026",
    anchor="none",
    weekday="none",
    week_offset="none",
    conf=0.95,
):
    """The seven answers the date battery reads; every answer carries its
    per-question confidence."""
    return {
        "mode": {"choice": mode, "confidence": conf},
        "month": {"choice": month, "confidence": conf},
        "day": {"choice": day, "confidence": conf},
        "year": {"choice": year, "confidence": conf},
        "day_anchor": {"choice": anchor, "confidence": conf},
        "weekday": {"choice": weekday, "confidence": conf},
        "week_offset": {"choice": week_offset, "confidence": conf},
    }


class _DatesDecider(MockDecider):
    """_high_decider plus scripted answers for the date battery (one
    Decision under each of the seven question ids) and a record of every
    request for wiring assertions."""

    def __init__(self, answers: dict) -> None:
        decision = Decision(applies=True, ok=True, answers=dict(answers), raw_tokens=60)
        verdicts = dict(_high_decider().verdicts)
        for qid in DATE_QIDS:
            verdicts[qid] = decision
        super().__init__(verdicts)
        self.requests: list[tuple[object, dict]] = []

    def decide(self, state, questions):
        self.requests.append((state, dict(questions)))
        return super().decide(state, questions)


def _date_requests(decider):
    """The date battery's requests among all the decider saw."""
    return [(state, q) for state, q in decider.requests if "mode" in q]


@respx.mock
def test_event_date_lands_in_evidence_data_observed_at_unchanged(tmp_path, monkeypatch):
    """Enforce: ONE date request for the claim-bearing doc; the assembled
    event_at threads into every candidate's evidence_data through the REAL
    normalize path while observed_at STAYS the document's fetched_at — the
    extracted date rides evidence_data only."""
    orch = FakeOrchLlm(docs=[_doc("doc-1", DOC_TEXT_1)])
    orch.config.storage.dossiers_dir = str(tmp_path / "dossiers")
    orch.snapshot = _snapshot()
    _patch_coverage(monkeypatch)
    decider = _DatesDecider(_dates_answers(month="8", day="20"))
    _patch_layer(monkeypatch, decide_cfg=_dates_cfg(), decider=decider)
    _patch_adapters(monkeypatch)
    _patch_planner(monkeypatch)
    _patch_implementer(monkeypatch, claims=[{"text": CLAIM_TEXT, "doc_id": "doc-1"}], fields={})
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", DATA_EXIT_CONSENT)

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, with_llm=True)

    stage = result["stages"]["implement"]
    assert stage["status"] == "ran"
    assert stage["claims_accepted"] == 1
    assert stage["event_dates_extracted"] == 1

    # exactly ONE date request, with the seven fixed question ids
    requests = _date_requests(decider)
    assert len(requests) == 1
    state, questions = requests[0]
    assert sorted(questions) == sorted(DATE_QIDS)
    assert state["doc_id"] == "doc-1"

    assert len(orch.signal_store.upserted) == 1
    sig = orch.signal_store.upserted[0]
    assert sig.signal_type == "llm_need"  # the deterministic type is untouched
    assert sig.evidence_data["event_at"] == "2026-08-20"
    assert "event_at_needs_review" not in sig.evidence_data
    # observed_at STAYS the document's fetched_at (normalized through the
    # REAL path): the event date never becomes the observed date
    assert sig.observed_at == "2026-08-15"

    rows = [
        json.loads(line)
        for line in Path(result["paths"]["decisions"]).read_text(encoding="utf-8").splitlines()
    ]
    date_rows = [r for r in rows if r["gate"] == "event_dates"]
    assert [r["outcome"] for r in date_rows] == ["extracted"]
    assert respx.calls.call_count == 0


@respx.mock
def test_event_date_only_for_claim_bearing_docs(tmp_path, monkeypatch):
    """ONLY docs with >= 1 accepted claim are dated: a doc whose bulk pass
    yields nothing costs NO date request (the yield-bounded fan-out)."""
    orch = FakeOrchLlm(docs=[_doc("doc-1", DOC_TEXT_1), _doc("doc-2", DOC_TEXT_1)])
    orch.config.storage.dossiers_dir = str(tmp_path / "dossiers")
    orch.snapshot = _snapshot()
    _patch_coverage(monkeypatch)

    def fake_call_implementer(messages, *, model, base_url, api_key, reasoning_effort=None,
                              timeout_s=60.0):
        if "Document doc_id: doc-1" in messages[1]["content"]:
            return {"claims": [{"text": CLAIM_TEXT, "doc_id": "doc-1"}]}
        return {"claims": []}

    monkeypatch.setattr(intel.llm_implement, "call_implementer", fake_call_implementer)
    decider = _DatesDecider(_dates_answers())
    _patch_layer(monkeypatch, decide_cfg=_dates_cfg(), decider=decider)
    _patch_adapters(monkeypatch)
    _patch_planner(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", DATA_EXIT_CONSENT)

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, with_llm=True)

    assert result["stages"]["implement"]["claims_accepted"] == 1
    requests = _date_requests(decider)
    assert len(requests) == 1  # doc-2 (zero accepted claims) made NO date request
    assert requests[0][0]["doc_id"] == "doc-1"
    assert result["stages"]["implement"]["event_dates_extracted"] == 1
    assert all(
        sig.evidence_data.get("doc_id") == "doc-1" for sig in orch.signal_store.upserted
    )
    assert respx.calls.call_count == 0


@respx.mock
def test_event_date_needs_review_marks_evidence_without_event_at(tmp_path, monkeypatch):
    """A review-routed date (mode=none here) threads event_at_needs_review
    into the evidence_data — and NEVER a fabricated event_at; the stage's
    extracted count stays 0."""
    orch = FakeOrchLlm(docs=[_doc("doc-1", DOC_TEXT_1)])
    orch.config.storage.dossiers_dir = str(tmp_path / "dossiers")
    orch.snapshot = _snapshot()
    _patch_coverage(monkeypatch)
    decider = _DatesDecider(_dates_answers(mode="none"))
    _patch_layer(monkeypatch, decide_cfg=_dates_cfg(), decider=decider)
    _patch_adapters(monkeypatch)
    _patch_planner(monkeypatch)
    _patch_implementer(monkeypatch, claims=[{"text": CLAIM_TEXT, "doc_id": "doc-1"}], fields={})
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", DATA_EXIT_CONSENT)

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, with_llm=True)

    # zero extractions: the stage record carries no count (the claims_typed
    # convention — the key appears only when N > 0)
    assert "event_dates_extracted" not in result["stages"]["implement"]
    assert len(orch.signal_store.upserted) == 1
    evidence = orch.signal_store.upserted[0].evidence_data
    assert evidence.get("event_at_needs_review") is True
    assert "event_at" not in evidence
    rows = [
        json.loads(line)
        for line in Path(result["paths"]["decisions"]).read_text(encoding="utf-8").splitlines()
    ]
    date_rows = [r for r in rows if r["gate"] == "event_dates"]
    assert [r["outcome"] for r in date_rows] == ["reviewed"]
    assert respx.calls.call_count == 0


@respx.mock
def test_event_date_disabled_gate_never_requests(tmp_path, monkeypatch):
    """The event_dates block absent (every pre-wave-2 config): NO date
    request, no evidence_data delta, no stage count — byte-identical."""
    orch = FakeOrchLlm(docs=[_doc("doc-1", DOC_TEXT_1)])
    orch.config.storage.dossiers_dir = str(tmp_path / "dossiers")
    orch.snapshot = _snapshot()
    _patch_coverage(monkeypatch)
    decider = _DatesDecider(_dates_answers())
    _patch_layer(monkeypatch, decide_cfg=_decide_cfg("enforce"), decider=decider)
    _patch_adapters(monkeypatch)
    _patch_planner(monkeypatch)
    _patch_implementer(monkeypatch, claims=[{"text": CLAIM_TEXT, "doc_id": "doc-1"}], fields={})
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", DATA_EXIT_CONSENT)

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, with_llm=True)

    assert result["stages"]["implement"]["claims_accepted"] == 1
    assert "event_dates_extracted" not in result["stages"]["implement"]
    assert _date_requests(decider) == []
    assert orch.signal_store.upserted
    evidence = orch.signal_store.upserted[0].evidence_data
    assert "event_at" not in evidence
    assert "event_at_needs_review" not in evidence
    assert respx.calls.call_count == 0


@respx.mock
def test_event_date_preflight_term_degrades_run_to_shadow(tmp_path, monkeypatch):
    """The pre-flight models the date battery BEFORE the first Jev call: a
    flat ~700-token allowance per staged doc (claim-bearing-ness is only
    known after G4, so every staged doc is counted — the documented honest
    upper bound). Two ~700-token docs over a 23000-token ceiling degrade
    ONLY because of the dates term; without the block the same run stays
    enforce."""
    body = "Acme breach quarter evaluating vendors. " * 70  # 2800 chars ~ 700 tokens
    decider = _DatesDecider(_dates_answers())
    orch = FakeOrchLlm(docs=[_doc("doc-1", body), _doc("doc-2", body)])
    orch.config.storage.dossiers_dir = str(tmp_path / "dossiers")
    orch.snapshot = _snapshot()
    _patch_coverage(monkeypatch)
    _patch_layer(
        monkeypatch,
        decide_cfg=_dates_cfg("enforce", max_decide_tokens_per_run=23000),
        decider=decider,
    )
    _patch_adapters(monkeypatch)
    _patch_planner(monkeypatch)
    _patch_implementer(monkeypatch, claims=[{"text": CLAIM_TEXT, "doc_id": "doc-1"}], fields={})
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", DATA_EXIT_CONSENT)

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, with_llm=True)

    # G4 (2x700) + doc gate (2x700) + dates (2x700) + allowances (19400):
    # the dates term alone pushes the projection over the ceiling.
    assert any(
        "decide token budget exceeded (23600 > 23000)" in gap for gap in result["gaps"]
    ), result["gaps"]
    assert result["decide"]["mode"] == "shadow"
    assert result["decide"]["degraded"] is True

    # The SAME run without the dates block stays under the ceiling: the term
    # is opt-in (cfgs without the block keep today's projection).
    orch = FakeOrchLlm(docs=[_doc("doc-1", body), _doc("doc-2", body)])
    orch.config.storage.dossiers_dir = str(tmp_path / "dossiers2")
    orch.snapshot = _snapshot()
    _patch_layer(
        monkeypatch,
        decide_cfg=_decide_cfg("enforce", max_decide_tokens_per_run=23000),
        decider=decider,
    )
    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, with_llm=True)
    assert result["decide"]["mode"] == "enforce"
    assert result["decide"]["degraded"] is False
    assert not any("token budget" in gap for gap in result["gaps"])
    assert respx.calls.call_count == 0


# ---------------------------------------------------------------------------
# 15. Value-extraction wiring: extract_claim_amount rides _implement_pass for
#     each ACCEPTED claim of a doc (after typing/dates in the enrichment
#     sequence); the normalized amount threads into THAT claim's candidate
#     evidence_data (enforce only; claims without money spans make NO
#     request and NO row)
# ---------------------------------------------------------------------------


#: Mirrors config/decide.yaml's wave-2 block (the battery's opt-in).
AMOUNTS_GATE_CFG = {"enabled": True, "on_error": "skip"}

#: A money-bearing claim that still passes the G4 lexical prefilter against
#: DOC_TEXT_1 (shares "Acme", "last", "quarter").
MONEY_CLAIM = "Acme raised $12.5M in Series B funding last quarter."


def _amounts_cfg(mode="enforce", **decider_overrides):
    """A decide cfg WITH the value_extraction block (the wave-2 opt-in: cfgs
    without the block keep today's behavior exactly)."""
    cfg = _decide_cfg(mode, **decider_overrides)
    cfg["decider"]["gates"]["value_extraction"] = dict(AMOUNTS_GATE_CFG)
    return cfg


class _AmountsDecider(MockDecider):
    """_high_decider plus a scripted value-extraction Decision under the
    'amount' prefix (answers BOTH amount_pick and amount_is_raise) and a
    record of every request for wiring assertions."""

    def __init__(self, pick: str = "$12.5M", raise_prob: float = 0.9) -> None:
        verdicts = dict(_high_decider().verdicts)
        verdicts["amount"] = Decision(
            applies=True,
            ok=True,
            answers={
                "amount_pick": {"choice": pick, "probabilities": {pick: 0.9}},
                "amount_is_raise": {"noul": raise_prob},
            },
            raw_tokens=30,
        )
        super().__init__(verdicts)
        self.requests: list[tuple[object, dict]] = []

    def decide(self, state, questions):
        self.requests.append((state, dict(questions)))
        return super().decide(state, questions)


def _amount_requests(decider):
    """The value-extraction requests among all the decider saw."""
    return [
        (state, questions)
        for state, questions in decider.requests
        if questions and all(qid.startswith("amount_") for qid in questions)
    ]


@respx.mock
def test_value_extraction_amounts_land_in_evidence_data(tmp_path, monkeypatch):
    """Enforce: ONE request for the money-bearing accepted claim; the picked
    span's normalized amount rides into that claim's candidate evidence_data
    (amount_display/amount_usd/amount_currency/amount_kind) through the REAL
    normalize path, and the stage record counts it."""
    orch = FakeOrchLlm(docs=[_doc("doc-1", DOC_TEXT_1)])
    orch.config.storage.dossiers_dir = str(tmp_path / "dossiers")
    orch.snapshot = _snapshot()
    _patch_coverage(monkeypatch)
    decider = _AmountsDecider(pick="$12.5M", raise_prob=0.9)
    _patch_layer(monkeypatch, decide_cfg=_amounts_cfg(), decider=decider)
    _patch_adapters(monkeypatch)
    _patch_planner(monkeypatch)
    _patch_implementer(monkeypatch, claims=[{"text": MONEY_CLAIM, "doc_id": "doc-1"}], fields={})
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", DATA_EXIT_CONSENT)

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, with_llm=True)

    stage = result["stages"]["implement"]
    assert stage["status"] == "ran"
    assert stage["claims_accepted"] == 1
    assert stage["amounts_extracted"] == 1

    requests = _amount_requests(decider)
    assert len(requests) == 1
    state, questions = requests[0]
    assert state["claim"] == MONEY_CLAIM
    assert state["candidates"] == ["$12.5M"]
    assert sorted(questions) == ["amount_is_raise", "amount_pick"]

    assert len(orch.signal_store.upserted) == 1
    sig = orch.signal_store.upserted[0]
    evidence = sig.evidence_data
    assert evidence["amount_display"] == "$12.5M"
    assert evidence["amount_usd"] == 12500000.0
    assert evidence["amount_currency"] == "USD"
    assert evidence["amount_kind"] == "raise"

    rows = [
        json.loads(line)
        for line in Path(result["paths"]["decisions"]).read_text(encoding="utf-8").splitlines()
    ]
    amount_rows = [r for r in rows if r["gate"] == "value_extraction"]
    assert [r["outcome"] for r in amount_rows] == ["extracted"]
    assert respx.calls.call_count == 0


@respx.mock
def test_value_extraction_claims_without_spans_make_no_request(tmp_path, monkeypatch):
    """The gate ENABLED but the claim carries no money spans: zero requests,
    zero rows, no amount fields, no stage count — the regex scan is free."""
    orch = FakeOrchLlm(docs=[_doc("doc-1", DOC_TEXT_1)])
    orch.config.storage.dossiers_dir = str(tmp_path / "dossiers")
    orch.snapshot = _snapshot()
    _patch_coverage(monkeypatch)
    decider = _AmountsDecider()  # would answer if asked
    _patch_layer(monkeypatch, decide_cfg=_amounts_cfg(), decider=decider)
    _patch_adapters(monkeypatch)
    _patch_planner(monkeypatch)
    _patch_implementer(monkeypatch, claims=[{"text": CLAIM_TEXT, "doc_id": "doc-1"}], fields={})
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", DATA_EXIT_CONSENT)

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, with_llm=True)

    stage = result["stages"]["implement"]
    assert stage["claims_accepted"] == 1
    assert "amounts_extracted" not in stage
    assert _amount_requests(decider) == []
    rows = [
        json.loads(line)
        for line in Path(result["paths"]["decisions"]).read_text(encoding="utf-8").splitlines()
    ]
    assert not [r for r in rows if r["gate"] == "value_extraction"]
    evidence = orch.signal_store.upserted[0].evidence_data
    assert "amount_display" not in evidence
    assert "amount_usd" not in evidence
    assert respx.calls.call_count == 0


@respx.mock
def test_value_extraction_disabled_gate_never_requests(tmp_path, monkeypatch):
    """The value_extraction block absent (every pre-wave-2 config): even a
    money-bearing claim costs NO request and lands NO amount fields —
    byte-identical to the pre-wave-2 behavior."""
    orch = FakeOrchLlm(docs=[_doc("doc-1", DOC_TEXT_1)])
    orch.config.storage.dossiers_dir = str(tmp_path / "dossiers")
    orch.snapshot = _snapshot()
    _patch_coverage(monkeypatch)
    decider = _AmountsDecider()
    _patch_layer(monkeypatch, decide_cfg=_decide_cfg("enforce"), decider=decider)
    _patch_adapters(monkeypatch)
    _patch_planner(monkeypatch)
    _patch_implementer(monkeypatch, claims=[{"text": MONEY_CLAIM, "doc_id": "doc-1"}], fields={})
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", DATA_EXIT_CONSENT)

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, with_llm=True)

    stage = result["stages"]["implement"]
    assert stage["claims_accepted"] == 1
    assert "amounts_extracted" not in stage
    assert _amount_requests(decider) == []
    evidence = orch.signal_store.upserted[0].evidence_data
    assert "amount_display" not in evidence
    assert "amount_usd" not in evidence
    assert respx.calls.call_count == 0


@respx.mock
def test_value_extraction_shadow_records_rows_but_no_amounts(tmp_path, monkeypatch):
    """Shadow: the battery runs (rows record what enforce WOULD do) but the
    extraction returns nothing to thread — enforce-only enrichment, a clean
    A/B; the stage record gains no count."""
    orch = FakeOrchLlm(docs=[_doc("doc-1", DOC_TEXT_1)])
    orch.config.storage.dossiers_dir = str(tmp_path / "dossiers")
    orch.snapshot = _snapshot()
    _patch_coverage(monkeypatch)
    decider = _AmountsDecider()
    _patch_layer(monkeypatch, decide_cfg=_amounts_cfg("shadow"), decider=decider)
    _patch_adapters(monkeypatch)
    _patch_planner(monkeypatch)
    _patch_implementer(monkeypatch, claims=[{"text": MONEY_CLAIM, "doc_id": "doc-1"}], fields={})
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", DATA_EXIT_CONSENT)

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, with_llm=True)

    assert result["decide"]["mode"] == "shadow"
    assert "amounts_extracted" not in result["stages"]["implement"]  # nothing bound
    assert len(_amount_requests(decider)) == 1  # the battery still runs
    rows = [
        json.loads(line)
        for line in Path(result["paths"]["decisions"]).read_text(encoding="utf-8").splitlines()
    ]
    amount_rows = [r for r in rows if r["gate"] == "value_extraction"]
    assert [r["outcome"] for r in amount_rows] == ["extracted"]  # would-be
    assert orch.signal_store.upserted == []  # shadow never promotes either
    assert respx.calls.call_count == 0


@respx.mock
def test_value_extraction_preflight_term_degrades_run_to_shadow(tmp_path, monkeypatch):
    """The pre-flight models value extraction BEFORE the first Jev call: a
    flat ~200-token allowance per staged doc (money-bearing-ness is only
    known after G4 + the regex scan, so every staged doc is counted — the
    documented honest upper bound). Two ~700-token docs over a 22500-token
    ceiling degrade ONLY because of the amounts term; without the block the
    same run stays enforce."""
    body = "Acme breach quarter evaluating vendors. " * 70  # 2800 chars ~ 700 tokens
    decider = _AmountsDecider()
    orch = FakeOrchLlm(docs=[_doc("doc-1", body), _doc("doc-2", body)])
    orch.config.storage.dossiers_dir = str(tmp_path / "dossiers")
    orch.snapshot = _snapshot()
    _patch_coverage(monkeypatch)
    _patch_layer(
        monkeypatch,
        decide_cfg=_amounts_cfg("enforce", max_decide_tokens_per_run=22500),
        decider=decider,
    )
    _patch_adapters(monkeypatch)
    _patch_planner(monkeypatch)
    _patch_implementer(monkeypatch, claims=[{"text": MONEY_CLAIM, "doc_id": "doc-1"}], fields={})
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", DATA_EXIT_CONSENT)

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, with_llm=True)

    # G4 (2x700) + doc gate (2x700) + amounts (2x200) + allowances (19400):
    # the amounts term alone pushes the projection over the ceiling.
    assert any(
        "decide token budget exceeded (22600 > 22500)" in gap for gap in result["gaps"]
    ), result["gaps"]
    assert result["decide"]["mode"] == "shadow"
    assert result["decide"]["degraded"] is True
    assert "amounts_extracted" not in result["stages"]["implement"]  # degraded: none bind

    # The SAME run without the value_extraction block stays under the
    # ceiling: the term is opt-in (cfgs without the block keep today's
    # projection).
    orch = FakeOrchLlm(docs=[_doc("doc-1", body), _doc("doc-2", body)])
    orch.config.storage.dossiers_dir = str(tmp_path / "dossiers2")
    orch.snapshot = _snapshot()
    _patch_layer(
        monkeypatch,
        decide_cfg=_decide_cfg("enforce", max_decide_tokens_per_run=23000),
        decider=decider,
    )
    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, with_llm=True)
    assert result["decide"]["mode"] == "enforce"
    assert result["decide"]["degraded"] is False
    assert not any("token budget" in gap for gap in result["gaps"])
    assert respx.calls.call_count == 0
