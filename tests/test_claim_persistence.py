"""Task 1 (A/B follow-up fix wave): opt-in claim persistence in G4 rows.

When ``lab.persist_claims`` is on (``config/decide.yaml``), every
``citation_soundness`` ledger row written by ``gate_citation_batch`` —
deterministic drops, Jev errors, verdicts (accepted/dropped/reviewed) —
carries the judged batch's claim payloads
(``[{"text", "quote_span", "doc_id"}, ...]``): the row's ``state_hash``
alone is not reversible, and the G4 wall study needs the claim texts
offline. The rows are enriched WHERE THEY ARE BUILT (never post-hoc —
``DecideLedger.record`` appends verbatim).

Off or absent (the shipped default): every ledger row stays byte-identical
to the pre-flag flow — pinned here by a serialization diff plus the exact
pre-change key set.

Fully offline: MockDecider + the FakeOrch bootstrap from
tests/test_intel_llm_layer.py (monkeypatched seams, respx asserts zero
HTTP calls). The wiring tests prove ``decide_cfg["lab"]["persist_claims"]``
reaches the gate rows through BOTH screen_and_gate_claims call sites in
``_implement_pass`` (the per-doc path and the five-fields path).
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import respx
import yaml

from src.core.config import Config
from src.core.models import Account, Document
from src.decide import Claim, DecideLedger, MockDecider, NullDecider, gate_citation_batch
from src.decide.jev import DATA_EXIT_CONSENT
from src.decide.shapes import Decision
from src.intel.snapshot import build_intelligence_snapshot
from src.pipeline import intel
from src.pipeline.runner import RunnerStats
from src.signals.lifecycle import load_supersede_map
from src.signals.taxonomy import Taxonomy

RUN = "run-claim-persist"

# --- gate-level fixtures (mirror tests/test_decide_gates.py) ----------------------

HEAD = "Nav header. Cookie banner. " * 60  # 1620 chars of boilerplate
MID = "The vendor disclosed a breach affecting 2.4 million records in March."
TAIL = "Filler sentence about nothing relevant. " * 150
DOC = HEAD + MID + " " + TAIL

GOOD_CLAIM = "vendor disclosed a breach affecting 2.4 million records"
SECOND_CLAIM = "Vendor disclosed breach records in March"
TAIL_CLAIM = "Filler sentence about nothing relevant"
BAD_CLAIM = "Quantum flux capacitor retro encabulator upgraded unprompted"

# The exact pre-change key set of a JUDGED (verdict-routed) citation row —
# the off-path shape the flag must never disturb.
INVARIANT_KEYS = {
    "gate",
    "boundary",
    "state_hash",
    "answers",
    "deterministic_action",
    "agree",
    "agree_direction",
    "error",
    "latency_ms",
    "raw_tokens",
    "model",
    "run_id",
}
JUDGED_ROW_KEYS = INVARIANT_KEYS | {"called", "outcome", "noul"}


def _payload(claims: list[Claim]) -> list[dict]:
    """The persisted claims payload — the exact list the rows must carry."""
    return [{"text": c.text, "quote_span": c.quote_span, "doc_id": c.doc_id} for c in claims]


def _choice_ans(label: str, prob: float) -> dict:
    return {"choice": label, "probabilities": {label: prob}}


def _batch_decider(answers: dict[str, dict], tokens: int = 120) -> MockDecider:
    return MockDecider(
        {"claim": Decision(applies=True, ok=True, answers=answers, raw_tokens=tokens)}
    )


# --- 1. flag ON: every row kind carries the exact batch payload ------------------


def test_flag_on_verdict_rows_carry_the_batch_claims():
    """Accepted, contradicted-dropped and says_nothing-reviewed rows alike
    carry the full judged batch's claims payload (texts + quote_spans +
    doc_id)."""
    ledger = DecideLedger()
    claims = [
        Claim(GOOD_CLAIM, "doc1", quote_span=GOOD_CLAIM),
        Claim(SECOND_CLAIM, "doc1"),
        Claim(TAIL_CLAIM, "doc1"),
    ]
    accepted, _ = gate_citation_batch(
        claims,
        "doc1",
        DOC,
        _batch_decider(
            {
                "claim_0": _choice_ans("supports", 0.9),
                "claim_1": _choice_ans("contradicted", 0.9),
                "claim_2": _choice_ans("says_nothing", 0.95),
            }
        ),
        {},
        ledger,
        RUN,
        "enforce",
        persist_claims=True,
    )
    assert accepted == claims[:1]  # supports above the floor is the only return
    outcomes = [(r["outcome"], r.get("reason")) for r in ledger.rows]
    assert outcomes == [
        ("accepted", None),
        ("dropped", "contradicted"),
        ("reviewed", "says_nothing"),
    ]
    expected = _payload(claims)
    assert all(r["claims"] == expected for r in ledger.rows)
    # The payload carries quote spans verbatim (None when absent).
    assert expected[0]["quote_span"] == GOOD_CLAIM
    assert expected[1]["quote_span"] is None
    assert {c["doc_id"] for c in expected} == {"doc1"}


def test_flag_on_deterministic_drop_rows_carry_the_batch_claims():
    """The pre-Jev deterministic drops (fabricated quote, missing document,
    prefilter, no decider) are rows gate_citation_batch writes — they carry
    the payload too."""
    # (a) fabricated quote-span: text anchors, the quote does not.
    claims = [Claim(GOOD_CLAIM, "doc1", quote_span="definitely not in the document")]
    ledger = DecideLedger()
    accepted, _ = gate_citation_batch(
        claims, "doc1", DOC, _batch_decider({}), {}, ledger, RUN, "enforce",
        persist_claims=True,
    )
    assert accepted == []
    assert ledger.rows[0]["reason"] == "fabricated_quote"
    assert ledger.rows[0]["claims"] == _payload(claims)

    # (b) missing document: every claim drops before any Jev spend.
    claims = [Claim(GOOD_CLAIM, "doc1")]
    ledger = DecideLedger()
    accepted, _ = gate_citation_batch(
        claims, "doc1", None, _batch_decider({}), {}, ledger, RUN, "enforce",
        persist_claims=True,
    )
    assert accepted == []
    assert ledger.rows[0]["reason"] == "no_document"
    assert ledger.rows[0]["claims"] == _payload(claims)

    # (c) lexical prefilter.
    claims = [Claim(BAD_CLAIM, "doc1")]
    ledger = DecideLedger()
    accepted, _ = gate_citation_batch(
        claims, "doc1", DOC, _batch_decider({}), {}, ledger, RUN, "enforce",
        persist_claims=True,
    )
    assert accepted == []
    assert ledger.rows[0]["reason"] == "prefilter"
    assert ledger.rows[0]["claims"] == _payload(claims)

    # (d) no decider: not-applicable fails closed, rows still audited.
    claims = [Claim(GOOD_CLAIM, "doc1")]
    ledger = DecideLedger()
    accepted, _ = gate_citation_batch(
        claims, "doc1", DOC, NullDecider(), {}, ledger, RUN, "enforce",
        persist_claims=True,
    )
    assert accepted == []
    assert ledger.rows[0]["reason"] == "no_decider"
    assert ledger.rows[0]["claims"] == _payload(claims)


def test_flag_on_jev_error_rows_carry_the_batch_claims():
    """The drop-all degradation rows on a Jev error carry the payload — the
    study's error stratum needs the claim texts exactly there."""
    ledger = DecideLedger()
    claims = [Claim(GOOD_CLAIM, "doc1"), Claim(SECOND_CLAIM, "doc1")]
    accepted, _ = gate_citation_batch(
        claims,
        "doc1",
        DOC,
        MockDecider({"claim": Decision(applies=True, ok=False, answers={}, raw_tokens=0)}),
        {},
        ledger,
        RUN,
        "enforce",
        persist_claims=True,
    )
    assert accepted == []
    assert len(ledger.rows) == 2
    assert all(r["outcome"] == "errored" for r in ledger.rows)
    assert all(r["claims"] == _payload(claims) for r in ledger.rows)


def test_flag_on_aggregate_detail_rows_stay_capped_copies():
    """The ledger's aggregate detail folds the enriched rows with its normal
    400-char text cap — enrichment lives in the ROWS, the aggregates just
    keep mirroring them."""
    ledger = DecideLedger()
    long_text = ("The vendor disclosed a breach affecting records " * 20).strip()
    claims = [Claim(long_text, "doc1")]
    gate_citation_batch(
        claims,
        "doc1",
        DOC,
        _batch_decider({"claim_0": _choice_ans("contradicted", 0.9)}),
        {},
        ledger,
        RUN,
        "enforce",
        persist_claims=True,
    )
    detail = ledger.aggregate("citation_soundness")["detail"]
    assert len(detail) == 1
    assert detail[0]["claims"][0]["text"] == long_text[:400]  # the house cap


# --- 2. flag OFF (the default): byte-identity ------------------------------------


def test_flag_off_default_judged_row_has_the_exact_pre_change_shape():
    """A judged row built with the param ABSENT carries no ``claims`` key and
    exactly the pre-change key set."""
    ledger = DecideLedger()
    claims = [Claim(GOOD_CLAIM, "doc1", quote_span=GOOD_CLAIM)]
    accepted, _ = gate_citation_batch(
        claims,
        "doc1",
        DOC,
        _batch_decider({"claim_0": _choice_ans("supports", 0.9)}),
        {},
        ledger,
        RUN,
        "enforce",  # persist_claims ABSENT = the shipped default
    )
    assert accepted == claims
    assert len(ledger.rows) == 1
    row = ledger.rows[0]
    assert "claims" not in row
    assert set(row) == JUDGED_ROW_KEYS


def test_flag_off_serialized_rows_byte_identical_default_vs_explicit_false():
    """Default call and explicit ``persist_claims=False`` serialize
    byte-identically — and deterministic-drop rows (every field static)
    serialize with no ``claims`` anywhere in the line."""

    def run(persist):
        ledger = DecideLedger()
        kwargs = {} if persist is None else {"persist_claims": persist}
        gate_citation_batch(
            [Claim(BAD_CLAIM, "doc1")], "doc1", DOC, _batch_decider({}), {}, ledger, RUN,
            "enforce", **kwargs
        )
        return [json.dumps(r, ensure_ascii=False, sort_keys=True, default=str) for r in ledger.rows]

    default_lines = run(None)
    off_lines = run(False)
    assert default_lines == off_lines
    assert default_lines and all("claims" not in line for line in default_lines)


def test_flag_off_judged_rows_identical_modulo_wall_clock_latency():
    """Judged rows too: apart from the measured latency, default and explicit
    False build identical rows."""

    def run(persist):
        ledger = DecideLedger()
        kwargs = {} if persist is None else {"persist_claims": persist}
        gate_citation_batch(
            [Claim(GOOD_CLAIM, "doc1")],
            "doc1",
            DOC,
            _batch_decider({"claim_0": _choice_ans("supports", 0.9)}),
            {},
            ledger,
            RUN,
            "enforce",
            **kwargs,
        )
        assert len(ledger.rows) == 1
        row = dict(ledger.rows[0])
        row.pop("latency_ms")  # wall clock, not shape
        return row

    assert run(None) == run(False)


# --- 3. wiring: decide_cfg lab.persist_claims -> gate rows end-to-end -------------

DOMAIN = "acme.com"
TODAY = date(2026, 8, 16)
TAX = Taxonomy.load("config/signals.yaml")
SIGNALS_CFG = yaml.safe_load(Path("config/signals.yaml").read_text(encoding="utf-8"))
SUPERSEDE = load_supersede_map(SIGNALS_CFG)
SCORING = yaml.safe_load(Path("config/scoring.yaml").read_text(encoding="utf-8"))
PLAYS = yaml.safe_load(Path("config/plays.yaml").read_text(encoding="utf-8"))

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


class _Registry:
    def __init__(self, accounts=None):
        self.accounts = dict(accounts or {})

    def get(self, domain):
        return self.accounts.get(domain)

    def upsert(self, account, *, source=None):
        self.accounts[account.domain] = account
        return account


class _FakeRawStore:
    """Metadata-only iter_docs / body-carrying get() (the real contract)."""

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
            yield replace(doc, body=None)

    def get(self, doc_id):
        return self._docs.get(doc_id)


class _FakeSignalStore:
    def __init__(self):
        self.upserted = []

    def upsert_many(self, signals):
        self.upserted.extend(signals)
        return len(signals), 0


class _FakeAdapter:
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


def _decide_cfg(mode="enforce", lab=None):
    """A decide.yaml-shaped dict (mirrors the shipped structure), optionally
    carrying the top-level ``lab`` block."""
    cfg = {
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
        "decider": {
            "model": "jev-test",
            "max_decide_tokens_per_run": 400000,
            "gates": {
                "plan_qualification": {"enabled": True, "floor": 0.70},
                "posture_audit": {"enabled": True},
                "routing": {"enabled": True, "route_threshold": 2000},
                "citation_soundness": {"enabled": True, "floor": 0.70},
                "need_promotion": {"enabled": True, "floor": 0.70},
            },
        },
    }
    if lab is not None:
        cfg["lab"] = dict(lab)
    return cfg


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


def _patch_layer(monkeypatch, *, decide_cfg, decider):
    monkeypatch.setattr(intel, "_load_decide_cfg", lambda cfg: dict(decide_cfg))
    monkeypatch.setattr(intel, "get_decider", lambda cfg: decider)
    monkeypatch.setattr(intel.decide_policy, "attach_decider", lambda handle: handle)


def _patch_coverage(monkeypatch):
    monkeypatch.setattr(intel, "build_coverage", lambda **kw: [])


def _patch_adapters(monkeypatch):
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
    def fake_generate_plan(**kw):
        return (plan, reason)

    monkeypatch.setattr(intel.llm_planner, "generate_plan", fake_generate_plan)


def _patch_implementer(monkeypatch, *, claims=None, fields=None):
    def fake_call_implementer(
        messages, *, model, base_url, api_key, reasoning_effort=None, timeout_s=60.0, **kw
    ):
        system = messages[0]["content"]
        if "REASONING IMPLEMENTER" in system:
            return fields
        return {"claims": claims}

    monkeypatch.setattr(intel.llm_implement, "call_implementer", fake_call_implementer)


def _run(monkeypatch, tmp_path, *, decide_cfg):
    """One full layer-active run; returns the parsed decisions.jsonl rows."""
    orch = FakeOrchLlm(docs=[_doc("doc-1", DOC_TEXT_1)])
    orch.config.storage.dossiers_dir = str(tmp_path / "dossiers")
    orch.snapshot = _snapshot()
    _patch_coverage(monkeypatch)
    _patch_layer(monkeypatch, decide_cfg=decide_cfg, decider=_high_decider())
    _patch_adapters(monkeypatch)
    _patch_planner(monkeypatch)
    _patch_implementer(
        monkeypatch, claims=[{"text": CLAIM_TEXT, "doc_id": "doc-1"}], fields={}
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", DATA_EXIT_CONSENT)

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, with_llm=True)
    rows = [
        json.loads(line)
        for line in Path(result["paths"]["decisions"]).read_text(encoding="utf-8").splitlines()
    ]
    return result, rows


@respx.mock
def test_wiring_lab_persist_claims_true_reaches_per_doc_gate_rows(tmp_path, monkeypatch):
    """decide_cfg lab.persist_claims=true flows _implement_pass -> the
    per-doc screen_and_gate_claims -> gate_citation_batch rows, end-to-end."""
    result, rows = _run(monkeypatch, tmp_path, decide_cfg=_decide_cfg("enforce", lab={"persist_claims": True}))

    assert result["stages"]["implement"]["claims_accepted"] == 1
    cite = [r for r in rows if r["gate"] == "citation_soundness"]
    assert cite, rows
    expected = [{"text": CLAIM_TEXT, "quote_span": None, "doc_id": "doc-1"}]
    assert all(r["claims"] == expected for r in cite)
    assert respx.calls.call_count == 0


@respx.mock
def test_wiring_lab_off_or_absent_leaves_gate_rows_unchanged(tmp_path, monkeypatch):
    """No lab block at all, and an explicit lab.persist_claims=false: the
    citation rows reach decisions.jsonl with no claims key (off-path
    byte-identity through the whole wiring)."""
    for cfg in (
        _decide_cfg("enforce"),                                # lab block absent
        _decide_cfg("enforce", lab={"persist_claims": False}),  # explicit off
    ):
        result, rows = _run(monkeypatch, tmp_path, decide_cfg=cfg)
        assert result["stages"]["implement"]["claims_accepted"] == 1
        cite = [r for r in rows if r["gate"] == "citation_soundness"]
        assert cite, rows
        assert all("claims" not in r for r in cite)


@respx.mock
def test_wiring_five_fields_path_persists_claims(tmp_path, monkeypatch):
    """The SECOND screen_and_gate_claims call site (the five dossier fields,
    judged as claims against the longest doc) persists its G4 rows too."""
    orch = FakeOrchLlm(docs=[_doc("doc-1", DOC_TEXT_1)])
    orch.config.storage.dossiers_dir = str(tmp_path / "dossiers")
    orch.snapshot = _snapshot()
    _patch_coverage(monkeypatch)
    _patch_layer(
        monkeypatch, decide_cfg=_decide_cfg("enforce", lab={"persist_claims": True}),
        decider=_high_decider(),
    )
    _patch_adapters(monkeypatch)
    _patch_planner(monkeypatch)
    # bulk claims EMPTY: every citation row below comes from the five-fields batch
    _patch_implementer(monkeypatch, claims=[], fields=FIVE_FIELDS)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", DATA_EXIT_CONSENT)

    result = intel.run_intel(DOMAIN, config=orch.config, orch=orch, with_llm=True)

    assert result["stages"]["implement"]["llm_fields"] is True
    rows = [
        json.loads(line)
        for line in Path(result["paths"]["decisions"]).read_text(encoding="utf-8").splitlines()
    ]
    cite = [r for r in rows if r["gate"] == "citation_soundness"]
    assert cite, rows
    for row in cite:
        assert {c["text"] for c in row["claims"]} == {CLAIM_TEXT, WHY_NOW_TEXT}
        assert all(c["doc_id"] == "doc-1" and c["quote_span"] is None for c in row["claims"])
    assert respx.calls.call_count == 0
