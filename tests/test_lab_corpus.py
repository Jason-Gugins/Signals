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
from src.core.doccontent import human_text
from src.core.rawstore import RawStore
from src.core.textutil import truncate
from src.decide.gates import state_hash
from src.lab.corpus import doc_gate_hashes, extract

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


def _gate_row(doc_id: str, account: str, run_id: str, *, text: str, raw_tokens: int, latency_ms: float, outcome: str = "included", state_hash_value: str | None = None) -> dict:
    """A document_gate ledger row: state_hash but NO doc_id (the fix-wave shape).

    ``state_hash_value`` overrides the recomputed raw-head digest — the
    extraction seam (Task 2b) means a post-extraction run's rows were hashed
    over the EXTRACTED text, which this fixture cannot recompute from
    ``text`` alone."""
    return {
        "gate": "document_gate",
        "boundary": "document_gate",
        "state_hash": state_hash_value
        or state_hash(
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


# --- Task 2b (extraction-before-claims): the dual-hash doc join ---------------
#
# Post-extraction runs hash document-gate states over the doccontent-EXTRACTED
# text (intel.py's decode seam); pre-extraction runs hashed the RAW stored
# body head. Both recomputers — this module's _DocIndex and
# scripts/ab/report.py::staged() (via the shared doc_gate_hashes helper) —
# must key BOTH hashes per doc so every package joins (measured drift
# surface: 43/75 rillet docs, 163+/226 databricks docs; RSS dominant).

RSS_BODY = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<rss version="2.0"><channel><title>Acme News</title>'
    "<item><title>Orbital Billing GA</title>"
    "<description>Acme ships Orbital Billing to every region.</description></item>"
    "<item><title>Series F</title>"
    "<description>Acme raises a Series F led by Northgate.</description></item>"
    "</channel></rss>"
)
RESULTS_BODY = json.dumps({
    "results": [
        {"artistName": "Acme Instruments", "trackName": "Orbital Billing",
         "description": "Track estimates and invoices in one place."},
        {"artistName": "Zibma Infotech", "trackName": "Rillet Education",
         "description": "An education quiz about everything."},
    ]
})

PKG_PRE = "acme.com-20261012T030000000000Z-ddd44404"
PKG_POST = "acme.com-20261012T040000000000Z-eee55505"


def _extracted_head(text: str, content_type: str, account: str) -> str:
    """The post-extraction gate-state head: byte-exactly what the pipeline
    hashed — doccontent.human_text(content_type, body, publisher_hint=account
    name)["text"], head-truncated to 9,600 (the state's source term IS the
    account name, so the recompute's publisher_hint is the account name)."""
    return truncate(
        human_text(content_type, text, publisher_hint=account)["text"], 9600
    ) or ""


def _envelope_world(tmp_path: Path):
    """One DB + raw store, two sniffer-classified envelope docs, packages on
    BOTH sides of the extraction seam over the SAME docs: PKG_PRE's gate rows
    carry the RAW-head hashes (pre-extraction run), PKG_POST's the
    EXTRACTED-head hashes (post-extraction run)."""
    db = Database(tmp_path / "s.db")
    rs = RawStore(db, tmp_path / "raw")
    db.upsert("accounts", {"domain": "acme.com", "name": "Acme"}, pk="domain")

    rss = rs.put(
        source="news_rss", url="https://acme.com/news/feed",
        body=RSS_BODY.encode("utf-8"), content_type="application/rss+xml",
        status=200, domain="acme.com",
    )
    results = rs.put(
        source="appstore", url="https://itunes.example/search",
        body=RESULTS_BODY.encode("utf-8"), content_type="text/javascript",
        status=200, domain="acme.com",
    )

    digests = {}
    for key, doc, body, ctype in (
        ("rss", rss, RSS_BODY, "application/rss+xml"),
        ("results", results, RESULTS_BODY, "text/javascript"),
    ):
        digests[key + "_raw"] = state_hash(
            {"doc_id": doc.doc_id, "source": "Acme", "text": truncate(body, 9600) or ""}
        )
        digests[key + "_ext"] = state_hash(
            {"doc_id": doc.doc_id, "source": "Acme",
             "text": _extracted_head(body, ctype, "Acme")}
        )

    dossiers = tmp_path / "dossiers"
    for pkg, run_id, rss_d, res_d in (
        (PKG_PRE, "ddd44404", digests["rss_raw"], digests["results_raw"]),
        (PKG_POST, "eee55505", digests["rss_ext"], digests["results_ext"]),
    ):
        _write_jsonl(dossiers / pkg / "decisions.jsonl", [
            _gate_row(rss.doc_id, "Acme", run_id, text="", raw_tokens=1500,
                      latency_ms=100.0, state_hash_value=rss_d),
            _gate_row(results.doc_id, "Acme", run_id, text="", raw_tokens=1600,
                      latency_ms=110.0, state_hash_value=res_d),
        ])
        (dossiers / pkg / "manifest.json").write_text(
            json.dumps({"domain": "acme.com"}), encoding="utf-8"
        )

    docs = {"rss": rss, "results": results}
    return db, rs, dossiers, docs, digests


def test_doc_gate_hashes_pre_extraction_world_is_raw_only(tmp_path):
    # A plain-text doc (pre-extraction world: no content_type, raw head ==
    # extracted head) yields exactly ONE hash — the raw one — so today's
    # joins are byte-identical to the pre-fix behavior.
    db, _rs, _dossiers, docs = _make_world(tmp_path)
    raw_digest = state_hash(
        {"doc_id": docs["doc1"].doc_id, "source": "Acme", "text": DOC1_HEAD}
    )
    assert doc_gate_hashes(docs["doc1"].doc_id, "Acme", DOC1_TEXT, "text/html") == [
        (raw_digest, DOC1_HEAD)
    ]
    # content_type-less rows (the oldest stores) take the same path.
    assert doc_gate_hashes(docs["doc2"].doc_id, "Acme", DOC2_TEXT, None) == [
        (
            state_hash({"doc_id": docs["doc2"].doc_id, "source": "Acme",
                        "text": DOC2_TEXT}),
            DOC2_TEXT,
        )
    ]
    assert db is not None  # the world builds; the helper is pure


def test_doc_gate_hashes_post_extraction_world_has_both_variants():
    # The RSS envelope's extracted head differs from its raw head: the helper
    # returns BOTH (digest, head) pairs — raw first (pre-extraction
    # compatibility), extracted second — and the extracted pair is
    # byte-exactly the state the post-extraction pipeline hashed.
    raw_pair = (
        state_hash({"doc_id": "d" * 64, "source": "Acme",
                    "text": truncate(RSS_BODY, 9600) or ""}),
        truncate(RSS_BODY, 9600) or "",
    )
    ext_head = _extracted_head(RSS_BODY, "application/rss+xml", "Acme")
    ext_pair = (
        state_hash({"doc_id": "d" * 64, "source": "Acme", "text": ext_head}),
        ext_head,
    )
    assert raw_pair[0] != ext_pair[0]  # the drift the fix joins over is real
    assert doc_gate_hashes("d" * 64, "Acme", RSS_BODY, "application/rss+xml") == [
        raw_pair,
        ext_pair,
    ]

    # json_results byte-consistency: the extracted head must reflect the SAME
    # publisher_hint the pipeline passed (the account name) — the Zibma
    # result's description is NOT in it, the Acme Instruments one is.
    head = _extracted_head(RESULTS_BODY, "text/javascript", "Acme")
    assert "Rillet Education" not in head
    assert "Track estimates and invoices in one place." in head
    pairs = doc_gate_hashes("e" * 64, "Acme", RESULTS_BODY, "text/javascript")
    assert pairs[1] == (
        state_hash({"doc_id": "e" * 64, "source": "Acme", "text": head}),
        head,
    )


def test_pre_and_post_extraction_packages_both_join(tmp_path):
    # The headline: one pre-extraction package (raw-head hashes) and one
    # post-extraction package (extracted-head hashes) over the SAME envelope
    # docs — BOTH packages' gate rows join, to the same doc_ids.
    db, _rs, dossiers, docs, digests = _envelope_world(tmp_path)
    assert digests["rss_raw"] != digests["rss_ext"]
    assert digests["results_raw"] != digests["results_ext"]

    rows = extract(db, dossiers, raw_dir=tmp_path / "raw")
    gate = [r for r in rows if r.get("gate") == "document_gate"]
    assert len(gate) == 4  # 2 docs x 2 packages
    assert all(r["doc_joined"] is True for r in gate)

    rss_id, res_id = docs["rss"].doc_id, docs["results"].doc_id
    by_run: dict[str, set] = {}
    for r in gate:
        by_run.setdefault(r["run_id"], set()).add(r["doc_id"])
    assert by_run["ddd44404"] == {rss_id, res_id}  # PKG_PRE joins via raw
    assert by_run["eee55505"] == {rss_id, res_id}  # PKG_POST joins via extracted

    # The joined head is the one THAT package's gate hashed — the
    # deterministic replay input, per variant, not one head for both.
    pre_rss = next(r for r in gate if r["run_id"] == "ddd44404" and r["doc_id"] == rss_id)
    post_rss = next(r for r in gate if r["run_id"] == "eee55505" and r["doc_id"] == rss_id)
    assert pre_rss["text"] == (truncate(RSS_BODY, 9600) or "")
    assert post_rss["text"] == _extracted_head(RSS_BODY, "application/rss+xml", "Acme")
    assert pre_rss["text"] != post_rss["text"]


def test_identical_heads_index_once_no_double_count(tmp_path):
    # html/text passthrough docs: raw head == extracted head ⇒ exactly one
    # (digest, head) pair per doc — the doc is indexed once, joins once.
    db, _rs, dossiers, docs = _make_world(tmp_path)
    rows = extract(db, dossiers, raw_dir=tmp_path / "raw")
    gate = [r for r in rows if r.get("gate") == "document_gate"]
    assert len(gate) == 5
    acme = [r for r in gate if r["run_id"] in ("aaa11101", "bbb22202")]
    assert len(acme) == 3
    assert all(r["doc_joined"] is True for r in acme)
    # doc1 was gated by BOTH acme packages: both rows join, same doc, and the
    # single-index behavior means one entry — no second, phantom doc.
    joined_doc1 = [r for r in gate if r.get("doc_id") == docs["doc1"].doc_id]
    assert len(joined_doc1) == 2
    assert len({r["text"] for r in joined_doc1}) == 1
    assert all(r["text"] == DOC1_HEAD for r in joined_doc1)


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
