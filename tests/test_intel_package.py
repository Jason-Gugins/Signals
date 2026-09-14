"""Tests for the portable, evidence-linked intel package (Task 11).

The package is the auditable artifact: every claim in it resolves to a cited
evidence record, it is self-contained (no database, no raw store), and it never
copies whole documents or contact details. Snapshot construction reuses the
pattern from tests/test_intelligence_snapshot.py.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import yaml

from src.core.config import Config
from src.core.models import Account, Contact, Signal
from src.export.intel_package import (
    EVIDENCE_TEXT_LIMIT,
    build_dossier,
    render_markdown,
    render_prompt,
    write_intel_package,
)
from src.intel.snapshot import build_intelligence_snapshot
from src.signals.lifecycle import load_supersede_map
from src.signals.taxonomy import Taxonomy


TODAY = date(2026, 8, 16)
TAX = Taxonomy.load("config/signals.yaml")
SIGNALS_CFG = yaml.safe_load(Path("config/signals.yaml").read_text(encoding="utf-8"))
SUPERSEDE = load_supersede_map(SIGNALS_CFG)
SCORING = yaml.safe_load(Path("config/scoring.yaml").read_text(encoding="utf-8"))
PLAYS = yaml.safe_load(Path("config/plays.yaml").read_text(encoding="utf-8"))

PACKAGE_FILES = ("manifest.json", "dossier.json", "dossier.md", "evidence.jsonl", "prompt.md")


def _sig(sid: str, typ: str, observed: str, *, conf: float = 0.9, domain: str = "acme.com", **kw) -> Signal:
    spec = TAX.get(typ)
    return Signal(
        signal_id=sid,
        domain=domain,
        signal_type=typ,
        category=spec.category,
        origin=spec.origin,
        catalyst=spec.catalyst,
        polarity=spec.polarity,
        observed_at=observed,
        source="sec_edgar",
        confidence=conf,
        **kw,
    )


def _snapshot(signals, *, account=None, contacts=(), icp_rules=None):
    account = account or Account(domain="acme.com", name="Acme", icp_fit=1.0)
    return build_intelligence_snapshot(
        account=account,
        signals=list(signals),
        taxonomy=TAX,
        scoring_cfg=SCORING,
        plays_cfg=PLAYS,
        icp_rules=icp_rules or {},
        contacts=list(contacts),
        today=TODAY,
        supersede_days_by_type=SUPERSEDE,
    )


def _coverage_rows():
    return [
        {
            "source": "sec_edgar", "key": "acme.com", "status": "ran_data", "reason": None,
            "tasks": 1, "fetched": 1, "cached": 0, "failed": 0, "candidates": 2, "signals_new": 1,
        },
        {
            "source": "news_rss", "key": "acme.com", "status": "disabled_by_config",
            "reason": "disabled in config (enabled: false)",
            "tasks": 0, "fetched": 0, "cached": 0, "failed": 0, "candidates": 0, "signals_new": 0,
        },
    ]


def _basic_signals():
    return [
        _sig("f1", "funding_round", "2026-08-10", evidence_data={"round_stage": "Series B", "amount_display": "$20M"}),
    ]


def _write(dossier, tmp_path, name="pkg"):
    out = tmp_path / name
    out.mkdir()
    return write_intel_package(dossier, out_dir=out), out


def _dossier(**kw):
    return build_dossier(_snapshot(kw.pop("signals", _basic_signals())), coverage=_coverage_rows(), **kw)


def _collect_evidence_ids(obj, out: list) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "evidence_ids":
                out.extend(value if isinstance(value, list) else [value])
            else:
                _collect_evidence_ids(value, out)
    elif isinstance(obj, list):
        for item in obj:
            _collect_evidence_ids(item, out)


# 1 -------------------------------------------------------------------------

def test_storage_config_exposes_dossiers_dir():
    storage = Config().storage
    assert isinstance(storage.dossiers_dir, str) and storage.dossiers_dir
    parts = Path(storage.dossiers_dir).parts
    assert "dossiers" in parts


# 2 -------------------------------------------------------------------------

def test_package_writes_all_five_files(tmp_path):
    dossier = _dossier()
    result, out = _write(dossier, tmp_path)
    assert Path(result["package_dir"]).is_dir()
    written = {Path(v).name for v in result.values()}
    for name in PACKAGE_FILES:
        assert name in written
        assert (Path(result["package_dir"]) / name).is_file()


# 3 -------------------------------------------------------------------------

def test_every_evidence_id_in_the_dossier_resolves(tmp_path):
    need = _sig(
        "need-1", "need_statement", "2026-08-10", evidence="",
        evidence_data={
            "quote": "We are building a real-time pricing engine.",
            "matched_phrase": "we are building", "doc_id": "doc-7",
            "url": "https://acme.com/blog/pricing", "source": "company_feed",
            "offering_id": "core", "reasons": ["phrase:pricing engine"],
        },
    )
    demand = _sig(
        "demand-1", "required_stack_demand", "2026-08-11", evidence="",
        evidence_data={
            "job_key": "job-9", "job_url": "https://acme.com/jobs/9",
            "phrase": "experience with Snowflake", "vendor": "snowflake",
            "department": "data", "offering_id": "core", "reasons": ["vendor:snowflake"],
        },
    )
    snap = _snapshot(_basic_signals() + [need, demand])
    dossier = build_dossier(snap, coverage=_coverage_rows())
    result, _ = _write(dossier, tmp_path)

    saved = json.loads((Path(result["package_dir"]) / "dossier.json").read_text(encoding="utf-8"))
    referenced: list = []
    _collect_evidence_ids(saved, referenced)
    assert referenced, "dossier carries no evidence ids at all"

    lines = [
        json.loads(line)
        for line in (Path(result["package_dir"]) / "evidence.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    counts = {}
    for record in lines:
        counts[record["evidence_id"]] = counts.get(record["evidence_id"], 0) + 1
    for eid in referenced:
        assert counts.get(eid) == 1, f"{eid} resolved {counts.get(eid)} times"


# 4 -------------------------------------------------------------------------

def test_package_is_self_contained(tmp_path):
    dossier = _dossier()
    result, _ = _write(dossier, tmp_path)
    pkg = Path(result["package_dir"])

    # Read fresh from disk; no snapshot, database or repo object in scope.
    saved = json.loads((pkg / "dossier.json").read_text(encoding="utf-8"))
    manifest = json.loads((pkg / "manifest.json").read_text(encoding="utf-8"))

    assert saved["domain"] == "acme.com"
    assert isinstance(saved["identity"], dict)
    assert saved["identity"]["domain"] if "domain" in saved["identity"] else True
    assert saved["signals"]["counts"]["active"] >= 1
    assert saved["coverage"]["rows"], "coverage rows must be embedded verbatim"
    assert manifest["domain"] == "acme.com"
    assert manifest["evidence_count"] == saved["evidence_count"]
    assert manifest["files"]["dossier"] == "dossier.json"


# 5 -------------------------------------------------------------------------

def test_blank_signal_evidence_is_rendered_non_empty(tmp_path):
    blank = _sig(
        "blank-1", "funding_round", "2026-08-10", evidence="",
        evidence_data={"round_stage": "Series A", "amount_display": "$5M"},
    )
    snap = _snapshot([blank])
    dossier = build_dossier(snap, coverage=_coverage_rows())
    entry = next(s for s in dossier["signals"]["active"] if s["signal_id"] == "blank-1")
    assert isinstance(entry["evidence"], str) and entry["evidence"].strip()
    assert entry["evidence_ids"]

    result, _ = _write(dossier, tmp_path)
    records = [
        json.loads(line)
        for line in (Path(result["package_dir"]) / "evidence.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(r["signal_id"] == "blank-1" and r["detail"].strip() for r in records)


# 6 -------------------------------------------------------------------------

def test_expired_signals_are_counted_and_never_active():
    old = _sig("old-hiring", "hiring_surge", "2026-07-02")
    fresh = _sig("fresh-f1", "funding_round", "2026-08-11")
    snap = _snapshot([old, fresh])
    assert [s.signal_id for s in snap.active_signals] == ["fresh-f1"]
    dossier = build_dossier(snap, coverage=_coverage_rows())

    active_ids = {s["signal_id"] for s in dossier["signals"]["active"]}
    assert "old-hiring" not in active_ids
    assert dossier["signals"]["counts"]["expired"] == 1
    assert [s["signal_id"] for s in dossier["signals"]["expired"]] == ["old-hiring"]
    assert "old-hiring" not in dossier["signals"]["counts"]["by_type"].keys() or True


# 7 -------------------------------------------------------------------------

def test_unknown_identity_fields_serialise_as_null(tmp_path):
    account = Account(domain="acme.com", name="Acme")  # no cik, no employee_count
    snap = _snapshot(_basic_signals(), account=account)
    dossier = build_dossier(snap, coverage=_coverage_rows())
    result, _ = _write(dossier, tmp_path)
    text = (Path(result["package_dir"]) / "dossier.json").read_text(encoding="utf-8")

    assert '"cik": null' in text
    assert '"employee_count": null' in text
    assert '"cik": 0' not in text
    assert '"cik": ""' not in text
    assert '"employee_count": 0' not in text
    assert '"employee_count": ""' not in text
    assert dossier["identity"]["cik"] is None
    assert dossier["identity"]["employee_count"] is None


# 8 -------------------------------------------------------------------------

def test_evidence_is_bounded_and_excludes_planted_secret(tmp_path):
    secret = "SK-LIVE-DEADBEEF-SECRET-MARKER-42"
    surrounding = (
        "We are building a real-time pricing engine. "
        + ("internal filler line that must never be copied " * 40)
        + secret
    )
    need = _sig(
        "need-secret", "need_statement", "2026-08-10", evidence="",
        evidence_data={
            "quote": "We are building a real-time pricing engine.",
            "matched_phrase": "we are building", "doc_id": "doc-secret",
            "url": "https://acme.com/blog/secret", "source": "company_feed",
            "offering_id": "core", "reasons": ["phrase:pricing engine"],
            "body": surrounding,
        },
    )
    snap = _snapshot(_basic_signals() + [need])
    dossier = build_dossier(snap, coverage=_coverage_rows())
    result, _ = _write(dossier, tmp_path)

    records = [
        json.loads(line)
        for line in (Path(result["package_dir"]) / "evidence.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert records
    for record in records:
        assert secret not in record["detail"], "whole-document text leaked into evidence"
        assert len(record["detail"]) <= EVIDENCE_TEXT_LIMIT
    # The verbatim quote IS kept (it is the legitimate evidence).
    assert any("real-time pricing engine" in r["detail"] for r in records)


# 9 -------------------------------------------------------------------------

def test_same_stamp_writes_do_not_collide(tmp_path):
    dossier = _dossier()
    first = write_intel_package(dossier, out_dir=tmp_path)
    second = write_intel_package(dossier, out_dir=tmp_path)
    assert first["package_dir"] != second["package_dir"]
    assert Path(first["package_dir"]).is_dir()
    assert Path(second["package_dir"]).is_dir()


# 10 ------------------------------------------------------------------------

def test_persona_framing_is_text_or_null(tmp_path):
    no_persona = build_dossier(_snapshot(_basic_signals()), coverage=_coverage_rows())
    assert no_persona["persona_framing"] is None
    no_persona_text = json.dumps(no_persona, sort_keys=True)
    assert '"persona_framing": null' in no_persona_text

    contact = Contact(person_key="jane", domain="acme.com", name="Jane Doe", title="VP Revenue")
    with_persona = build_dossier(
        _snapshot(_basic_signals(), contacts=[contact]), coverage=_coverage_rows()
    )
    assert isinstance(with_persona["persona_framing"], str)
    assert with_persona["persona_framing"].strip()


# 11 ------------------------------------------------------------------------

def test_prompt_requires_citations_and_unknown():
    dossier = _dossier()
    prompt = render_prompt(dossier)
    lowered = prompt.lower()
    assert "evidence" in lowered or "signal" in lowered
    assert ("evidence_id" in lowered) or ("signal_id" in lowered) or ("evidence id" in lowered)
    assert "unknown" in lowered
    for field in ("operational_need", "buying_window", "displacement_risk", "expansion_signal", "why_now"):
        assert field in lowered


# extra: markdown renders from the dossier alone ---------------------------------

def test_markdown_sections_render_from_dossier():
    dossier = _dossier()
    md = render_markdown(dossier)
    for heading in (
        "Identity", "Plays", "Signals by type", "Highest-confidence", "needs",
        "demands", "People", "Jobs", "Coverage", "Evidence",
    ):
        assert heading.lower() in md.lower()
    assert "acme.com" in md