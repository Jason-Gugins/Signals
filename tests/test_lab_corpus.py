"""Task 1 (decide-lab): the ledger corpus extractor.

Spec: docs/plans/2026-10-11-autoresearch-decide-lab.md — walk
``<dossiers_dir>/<pkg>/decisions.jsonl`` (+ ``evidence.jsonl``), join
``document_gate`` rows to raw-store texts, attach doc metadata + the
dossier's market_profile_id, and emit one flat dict per row.

The fixture world is built programmatically (tmp DB + RawStore + synthetic
package dirs): the raw docs are gzipped binaries, so committing them as
static fixtures buys nothing. A smoke test against one real harvest package
runs only when that package (and the real DB) is present — offline and fast.

Post-fix-wave fact under test: gate rows carry ``state_hash`` but NOT
``doc_id`` — the join is the state-hash reconstruction proven in
scripts/ab/report.py (state = {"doc_id", "source": account_name, "text":
truncate(text, 9600)}), with the account name read from the accounts table.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path

import pytest

from src.core.db import Database
from src.core.rawstore import RawStore
from src.core.textutil import truncate
from src.decide.gates import state_hash
from src.lab.corpus import extract

REPO = Path(__file__).resolve().parents[1]

# > 9600 chars so the gate-state head-truncation path is exercised.
DOC1_TEXT = "Acme Ships Orbital Billing 2.0. " + ("lorem ipsum dolor sit amet " * 400)
DOC2_TEXT = "Acme raises Series F, confirms orbital billing GA."
DOC3_TEXT = "Beta Labs launches retry planner for flaky pipelines."
DOC1_HEAD = truncate(DOC1_TEXT, 9600) or ""

PKG_A = "acme.com-20261012T000000000000Z-aaa11101"
PKG_B = "acme.com-20261012T010000000000Z-bbb22202"
PKG_C = "beta.io-20261012T020000000000Z-ccc33303"


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )


def _gate_row(doc_id: str, account: str, run_id: str, *, text: str, raw_tokens: int, latency_ms: float, outcome: str = "included") -> dict:
    """A document_gate ledger row: state_hash but NO doc_id (the fix-wave shape)."""
    return {
        "gate": "document_gate",
        "boundary": "document_gate",
        "state_hash": state_hash(
            {"doc_id": doc_id, "source": account, "text": truncate(text, 9600) or ""}
        ),
        "answers": {"is_relevant": {"type": "noul", "noul": 0.9}},
        "deterministic_action": "include",
        "agree": None,
        "agree_direction": None,
        "error": None,
        "latency_ms": latency_ms,
        "raw_tokens": raw_tokens,
        "model": "jev-1.13.0",
        "run_id": run_id,
        "called": True,
        "outcome": outcome,
        "reason": None,
    }


def _citation_row(run_id: str, *, claims: list[dict] | None = None, outcome: str = "kept") -> dict:
    row = {
        "gate": "citation_soundness",
        "boundary": "citation_soundness",
        "state_hash": "deadbeef" * 8,
        "answers": None,
        "deterministic_action": "accept",
        "agree": None,
        "agree_direction": None,
        "error": None,
        "latency_ms": 210.5,
        "raw_tokens": 3791,
        "model": "jev-1.13.0",
        "run_id": run_id,
        "called": True,
        "outcome": outcome,
        "noul": 0.91,
        "reason": "quote_anchored",
    }
    if claims is not None:
        row["claims"] = claims
    return row


def _make_world(tmp_path: Path):
    """One DB + raw store, three packages across two domains, three docs."""
    db = Database(tmp_path / "s.db")
    rs = RawStore(db, tmp_path / "raw")
    db.upsert("accounts", {"domain": "acme.com", "name": "Acme"}, pk="domain")
    db.upsert("accounts", {"domain": "beta.io", "name": "Beta Labs"}, pk="domain")

    doc1 = rs.put(
        source="blog", url="https://acme.com/blog/orbital-billing",
        body=DOC1_TEXT.encode("utf-8"), content_type="text/html", status=200,
        domain="acme.com",
    )
    doc2 = rs.put(
        source="news_rss", url="https://acme.com/news/series-f",
        body=DOC2_TEXT.encode("utf-8"), content_type="text/html", status=200,
        domain="acme.com",
    )
    doc3 = rs.put(
        source="blog", url="https://beta.io/blog/retry-planner",
        body=DOC3_TEXT.encode("utf-8"), content_type="text/html", status=200,
        domain="beta.io",
    )

    claim1 = {
        "text": "Acme shipped Orbital Billing 2.0 with usage tiering.",
        "quote_span": "Acme Ships Orbital Billing 2.0.",
        "doc_id": doc1.doc_id,
    }
    claim2 = {
        "text": "Acme raised a Series F.",
        "quote_span": "Acme raises Series F",
        "doc_id": doc2.doc_id,
    }

    dossiers = tmp_path / "dossiers"

    # Package A (acme run 1): 2 gate rows + 2 citation rows + 1 routing row.
    _write_jsonl(
        dossiers / PKG_A / "decisions.jsonl",
        [
            _gate_row(doc1.doc_id, "Acme", "aaa11101", text=DOC1_TEXT,
                      raw_tokens=3821, latency_ms=1220.054),
            _gate_row(doc2.doc_id, "Acme", "aaa11101", text=DOC2_TEXT,
                      raw_tokens=1900, latency_ms=141.0, outcome="excluded"),
            _citation_row("aaa11101", claims=[claim1, claim2]),
            _citation_row("aaa11101", outcome="reviewed"),
            {"gate": "routing", "boundary": "routing", "state_hash": "cafe" * 16,
             "answers": None, "deterministic_action": "proceed", "agree": None,
             "agree_direction": None, "error": None, "latency_ms": 0.0,
             "raw_tokens": 0, "model": None, "run_id": "aaa11101", "called": False},
        ],
    )
    _write_jsonl(
        dossiers / PKG_A / "evidence.jsonl",
        [
            {"evidence_id": "ev-0001", "kind": "signal", "observed_at": "2026-10-12",
             "signal_id": "c1605c6b00d460c8", "source": "jobsignals", "url": None,
             "detail": "7 new Commercial Sales roles posted"},
            {"evidence_id": "ev-0002", "kind": "signal", "observed_at": "2026-10-12",
             "signal_id": "d0705c6b00d460c8", "source": "blog", "url": "https://acme.com/blog/orbital-billing",
             "detail": "Orbital Billing 2.0 announced"},
        ],
    )
    (dossiers / PKG_A / "manifest.json").write_text(json.dumps({
        "domain": "acme.com", "market_profile_id": "ab_testing_profile",
        "generated_at": "2026-10-12T00:01:02+00:00", "name": "Acme",
    }), encoding="utf-8")

    # Package B (acme run 2): the same long doc gated again + a claims row.
    _write_jsonl(
        dossiers / PKG_B / "decisions.jsonl",
        [
            _gate_row(doc1.doc_id, "Acme", "bbb22202", text=DOC1_TEXT,
                      raw_tokens=3830, latency_ms=150.0),
            _citation_row("bbb22202", claims=[claim1]),
        ],
    )
    (dossiers / PKG_B / "manifest.json").write_text(json.dumps({
        "domain": "acme.com", "market_profile_id": "ab_testing_profile",
        "generated_at": "2026-10-12T01:01:02+00:00", "name": "Acme",
    }), encoding="utf-8")

    # Package C (beta): one joinable gate row, one bogus-hash row (doc pruned
    # from the store), one citation row without claims; no market profile id.
    _write_jsonl(
        dossiers / PKG_C / "decisions.jsonl",
        [
            _gate_row(doc3.doc_id, "Beta Labs", "ccc33303", text=DOC3_TEXT,
                      raw_tokens=800, latency_ms=90.25),
            _gate_row("0" * 64, "Beta Labs", "ccc33303", text="gone from store",
                      raw_tokens=700, latency_ms=80.0),
            _citation_row("ccc33303", outcome="dropped"),
        ],
    )
    (dossiers / PKG_C / "manifest.json").write_text(json.dumps({
        "domain": "beta.io", "market_profile_id": None,
        "generated_at": "2026-10-12T02:01:02+00:00", "name": "Beta Labs",
    }), encoding="utf-8")

    docs = {"doc1": doc1, "doc2": doc2, "doc3": doc3}
    return db, rs, dossiers, docs


def test_row_counts_and_row_types(tmp_path):
    db, _rs, dossiers, _docs = _make_world(tmp_path)
    rows = extract(db, dossiers, raw_dir=tmp_path / "raw")
    # pkg A: 5 decisions + 2 evidence; pkg B: 2; pkg C: 3 → 10 + 2 = 12.
    assert len(rows) == 12
    assert Counter(r["row_type"] for r in rows) == {"decision": 10, "evidence": 2}
    # Deterministic order: packages by name, rows in file order.
    assert [r["package"] for r in rows] == [PKG_A] * 7 + [PKG_B] * 2 + [PKG_C] * 3


def test_document_gate_hash_join(tmp_path):
    db, _rs, dossiers, docs = _make_world(tmp_path)
    rows = extract(db, dossiers, raw_dir=tmp_path / "raw")
    gate = [r for r in rows if r.get("gate") == "document_gate"]
    assert len(gate) == 5  # A: 2, B: 1 (same doc re-gated), C: 2

    doc1, doc2, doc3 = docs["doc1"], docs["doc2"], docs["doc3"]
    g1 = next(r for r in gate if r["run_id"] == "aaa11101" and r.get("byte_size") == len(DOC1_TEXT.encode("utf-8")))
    assert g1["doc_id"] == doc1.doc_id
    assert g1["doc_joined"] is True
    assert g1["text"] == DOC1_HEAD  # the 9600-char head the gate hashed
    assert g1["url"] == doc1.url
    assert g1["source"] == "blog"
    assert g1["fetched_at"] == doc1.fetched_at
    assert g1["domain"] == "acme.com"
    assert g1["market_profile_id"] == "ab_testing_profile"
    assert g1["account"] == "Acme"

    g2 = next(r for r in gate if r["run_id"] == "aaa11101" and r.get("doc_id") == doc2.doc_id)
    assert g2["text"] == DOC2_TEXT  # short doc: head == full text
    assert g2["source"] == "news_rss"

    # The re-gated long doc in run B joins to the same doc_id.
    g_b = next(r for r in gate if r["run_id"] == "bbb22202")
    assert g_b["doc_id"] == doc1.doc_id
    assert g_b["text"] == DOC1_HEAD

    g3 = next(r for r in gate if r["run_id"] == "ccc33303" and r["doc_joined"])
    assert g3["doc_id"] == doc3.doc_id
    assert g3["account"] == "Beta Labs"
    assert g3["market_profile_id"] is None  # beta manifest has no profile

    # The bogus-hash row (doc no longer in the store) is kept, honestly unjoined.
    bogus = [r for r in gate if r["run_id"] == "ccc33303" and not r["doc_joined"]]
    assert len(bogus) == 1
    assert bogus[0]["doc_id"] is None
    assert "text" not in bogus[0]


def test_citation_claims_passthrough(tmp_path):
    db, _rs, dossiers, docs = _make_world(tmp_path)
    rows = extract(db, dossiers, raw_dir=tmp_path / "raw")
    cites = [r for r in rows if r.get("gate") == "citation_soundness"]
    assert len(cites) == 4

    with_claims = [r for r in cites if "claims" in r]
    assert len(with_claims) == 2
    first = next(r for r in with_claims if r["run_id"] == "aaa11101")
    assert first["claims"] == [
        {"text": "Acme shipped Orbital Billing 2.0 with usage tiering.",
         "quote_span": "Acme Ships Orbital Billing 2.0.",
         "doc_id": docs["doc1"].doc_id},
        {"text": "Acme raised a Series F.",
         "quote_span": "Acme raises Series F",
         "doc_id": docs["doc2"].doc_id},
    ]
    # Citation rows carry no doc join of their own — claims are the doc link.
    assert all(r.get("doc_id") is None for r in cites)
    # Pre-persistence rows (no claims key) surface without one.
    without = [r for r in cites if "claims" not in r]
    assert len(without) == 2
    assert {r["outcome"] for r in without} == {"reviewed", "dropped"}


def test_domain_filtering(tmp_path):
    db, _rs, dossiers, _docs = _make_world(tmp_path)
    all_rows = extract(db, dossiers, raw_dir=tmp_path / "raw")
    acme = extract(db, dossiers, domains=["acme.com"], raw_dir=tmp_path / "raw")
    beta = extract(db, dossiers, domains=["beta.io"], raw_dir=tmp_path / "raw")
    assert len(acme) == 9 and {r["domain"] for r in acme} == {"acme.com"}
    assert len(beta) == 3 and {r["package"] for r in beta} == {PKG_C}
    assert len(extract(db, dossiers, domains=["acme.com", "beta.io"],
                       raw_dir=tmp_path / "raw")) == len(all_rows)
    assert extract(db, dossiers, domains=["nobody.example"],
                   raw_dir=tmp_path / "raw") == []


def test_cost_fields_passthrough(tmp_path):
    db, _rs, dossiers, _docs = _make_world(tmp_path)
    rows = extract(db, dossiers, raw_dir=tmp_path / "raw")
    decisions = [r for r in rows if r["row_type"] == "decision"]
    assert decisions
    assert all("raw_tokens" in r and "latency_ms" in r for r in decisions)
    g1 = next(r for r in decisions if r["gate"] == "document_gate" and r["run_id"] == "aaa11101"
              and r.get("doc_id") is not None and r["source"] == "blog")
    assert g1["raw_tokens"] == 3821
    assert g1["latency_ms"] == 1220.054


def test_evidence_rows_carry_package_metadata(tmp_path):
    db, _rs, dossiers, _docs = _make_world(tmp_path)
    rows = extract(db, dossiers, raw_dir=tmp_path / "raw")
    ev = [r for r in rows if r["row_type"] == "evidence"]
    assert len(ev) == 2
    assert {r["evidence_id"] for r in ev} == {"ev-0001", "ev-0002"}
    assert all(r["domain"] == "acme.com" for r in ev)
    assert all(r["market_profile_id"] == "ab_testing_profile" for r in ev)
    assert all(r["run_id"] == "aaa11101" for r in ev)  # from the package name


# --- real-package smoke (offline, fast; skipped when data is absent) ----------

REAL_PKG = REPO / "data" / "dossiers" / "databricks.com-20261012T183117362656Z-57504658"
REAL_DB = REPO / "data" / "signals.db"

_have_real = REAL_PKG.exists() and (REAL_PKG / "decisions.jsonl").exists() and REAL_DB.exists()


class _ReadOnlyDB:
    """Duck-typed read-only stand-in for Database (no migrations, no WAL)."""

    def __init__(self, path: Path):
        self._conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self._conn.row_factory = sqlite3.Row

    def one(self, sql, params=()):
        row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def query(self, sql, params=()):
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]


@pytest.mark.skipif(not _have_real, reason="real harvest package/DB not present")
def test_smoke_real_harvest_package():
    rows = extract(
        _ReadOnlyDB(REAL_DB), REPO / "data" / "dossiers",
        domains=["databricks.com"], raw_dir=REPO / "data" / "raw",
    )
    pkg_rows = [r for r in rows if r["package"] == REAL_PKG.name]
    assert pkg_rows

    gate = [r for r in pkg_rows if r.get("gate") == "document_gate"]
    assert gate
    # Every real gate row joins via the state-hash reconstruction.
    assert all(r["doc_joined"] for r in gate)
    assert all(r["doc_id"] and r["text"] and len(r["text"]) <= 9601 for r in gate)
    assert all(r["byte_size"] and r["fetched_at"] for r in gate)
    assert all(r["domain"] == "databricks.com" and r["account"] == "Databricks" for r in gate)

    # This harvest package ran with lab.persist_claims on: claims surface.
    cites = [r for r in pkg_rows if r.get("gate") == "citation_soundness"]
    assert cites
    assert all("claims" in r and r["claims"] for r in cites)

    assert all(r["raw_tokens"] is not None and r["latency_ms"] is not None
               for r in pkg_rows if r["row_type"] == "decision")
