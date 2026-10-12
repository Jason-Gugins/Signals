"""Task 2 Step 2 (decide-lab): the budgeted claim backfill replay.

Spec: docs/plans/2026-10-11-autoresearch-decide-lab.md — for corpus docs whose
claims are missing (pre-persistence citation rows, or docs the citation gate
never judged), re-run the bulk implementer (same decide.yaml
``implementers.bulk`` model, same ``build_extraction_messages`` prompt — the
post-fix-wave prompt that FORBIDS envelope/meta claims, which is the point of
replaying) and cache the regenerated claims to ``<out_dir>/<doc_id>.json``
(cache-forever). Hard bounds: ``max_docs`` per invocation and ``budget_calls``
(a call counter checked BEFORE each call, never exceeded mid-flight). Never
raises past a doc.

Fully offline: the implementer is a fake double monkeypatched onto
``src.llm.implement.call_implementer``; no test spends a token. The fixture
world mirrors tests/test_lab_corpus.py (tmp DB + RawStore + synthetic
package dirs) — candidates are found by reusing src/lab/corpus.py.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.core.config import Config
from src.core.db import Database
from src.core.rawstore import RawStore
from src.core.textutil import truncate
from src.decide.gates import state_hash
from src.decide.jev import DATA_EXIT_CONSENT
from src.lab.backfill import backfill

# > 9600 chars so the corpus join's head-truncation differs from the full
# body — the implementer must see the FULL text, not the gate-state head.
DOC1_TEXT = "Acme Ships Orbital Billing 2.0. " + ("lorem ipsum dolor sit amet " * 400)
DOC2_TEXT = "Acme raises Series F, confirms orbital billing GA."
DOC3_TEXT = "Acme publishes the Orbital Billing changelog weekly."
DOC4_TEXT = "Beta Labs launches a retry planner for flaky pipelines."

PKG_A = "acme.com-20261012T000000000000Z-aaa11101"
PKG_B = "acme.com-20261012T010000000000Z-bbb22202"
PKG_C = "beta.io-20261012T020000000000Z-ccc33303"

ACCOUNT_ACME = "Acme"
ACCOUNT_BETA = "Beta Labs"


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _gate_row(doc_id: str, account: str, run_id: str, *, text: str, outcome: str = "included") -> dict:
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
        "latency_ms": 300.0,
        "raw_tokens": 1200,
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
        "reason": "quote_anchored",
    }
    if claims is not None:
        row["claims"] = claims
    return row


def _manifest(pkg: Path, domain: str) -> None:
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "manifest.json").write_text(
        json.dumps({"domain": domain, "generated_at": "2026-10-12T00:00:00+00:00"}),
        encoding="utf-8",
    )


def _make_world(tmp_path: Path):
    """One DB + raw store, three packages, four docs.

    Candidate backlog (corpus order, docs whose claims are missing):
    [doc1, doc2, doc4]. doc3 is NOT a candidate — its package persisted its
    claims (the opt-in lab.persist_claims shape).
    """
    db = Database(tmp_path / "s.db")
    rs = RawStore(db, tmp_path / "raw")
    db.upsert("accounts", {"domain": "acme.com", "name": ACCOUNT_ACME}, pk="domain")
    db.upsert("accounts", {"domain": "beta.io", "name": ACCOUNT_BETA}, pk="domain")

    doc1 = rs.put(source="blog", url="https://acme.com/blog/orbital-billing",
                  body=DOC1_TEXT.encode("utf-8"), content_type="text/html",
                  status=200, domain="acme.com")
    doc2 = rs.put(source="news_rss", url="https://acme.com/news/series-f",
                  body=DOC2_TEXT.encode("utf-8"), content_type="text/html",
                  status=200, domain="acme.com")
    doc3 = rs.put(source="blog", url="https://acme.com/blog/changelog",
                  body=DOC3_TEXT.encode("utf-8"), content_type="text/html",
                  status=200, domain="acme.com")
    doc4 = rs.put(source="blog", url="https://beta.io/blog/retry-planner",
                  body=DOC4_TEXT.encode("utf-8"), content_type="text/html",
                  status=200, domain="beta.io")

    dossiers = tmp_path / "dossiers"

    # Package A (acme, pre-persistence): gate rows for doc1/doc2, citation
    # rows WITHOUT persisted claims.
    _manifest(dossiers / PKG_A, "acme.com")
    _write_jsonl(
        dossiers / PKG_A / "decisions.jsonl",
        [
            _gate_row(doc1.doc_id, ACCOUNT_ACME, "aaa11101", text=DOC1_TEXT),
            _gate_row(doc2.doc_id, ACCOUNT_ACME, "aaa11101", text=DOC2_TEXT, outcome="excluded"),
            _citation_row("aaa11101", outcome="reviewed"),
            _citation_row("aaa11101", outcome="dropped"),
        ],
    )

    # Package B (acme): doc3 gated AND claims persisted — not a candidate.
    _manifest(dossiers / PKG_B, "acme.com")
    _write_jsonl(
        dossiers / PKG_B / "decisions.jsonl",
        [
            _gate_row(doc3.doc_id, ACCOUNT_ACME, "bbb22202", text=DOC3_TEXT),
            _citation_row("bbb22202", claims=[
                {"text": "Acme publishes the changelog weekly.",
                 "quote_span": "publishes the Orbital Billing changelog weekly",
                 "doc_id": doc3.doc_id},
            ]),
        ],
    )

    # Package C (beta): doc4 gated but never citation-judged; plus one
    # bogus-hash gate row (doc pruned from the store) that must be ignored.
    _manifest(dossiers / PKG_C, "beta.io")
    _write_jsonl(
        dossiers / PKG_C / "decisions.jsonl",
        [
            _gate_row(doc4.doc_id, ACCOUNT_BETA, "ccc33303", text=DOC4_TEXT),
            _gate_row("0" * 64, ACCOUNT_BETA, "ccc33303", text="gone from store"),
        ],
    )

    docs = {"doc1": doc1, "doc2": doc2, "doc3": doc3, "doc4": doc4}
    return db, rs, dossiers, docs


class FakeImplementer:
    """A call_implementer double: records every call, serves canned payloads
    per doc_id (None = the failed-call seam; an Exception raises, to prove
    backfill never raises past a doc)."""

    def __init__(self, payloads: dict[str, object] | None = None, default: object = None):
        self.payloads = payloads or {}
        self.default = default
        self.calls: list[dict] = []

    def __call__(self, messages, **kwargs):
        user = messages[1]["content"]
        doc_id = user.split("Document doc_id: ", 1)[1].split("\n", 1)[0]
        self.calls.append({"doc_id": doc_id, "messages": messages, **kwargs})
        result = self.payloads.get(doc_id, self.default)
        if isinstance(result, Exception):
            raise result
        return result

    @property
    def doc_ids(self) -> list[str]:
        return [c["doc_id"] for c in self.calls]


class FlakyStore:
    """A raw_store stand-in for the unreadable-body window: the corpus scan
    reads the REAL files (raw_dir delegates), but get() raises for some docs
    and returns a body-less Document for others — the store state the loop
    can see even though the scan joined (file deleted/corrupted in between,
    or a body that decodes empty)."""

    def __init__(self, rs: RawStore, raise_for: tuple[str, ...] = (), empty_for: tuple[str, ...] = ()):
        self._rs = rs
        self.raw_dir = rs.raw_dir
        self._raise_for = set(raise_for)
        self._empty_for = set(empty_for)

    def get(self, doc_id: str):
        if doc_id in self._raise_for:
            raise OSError(f"storage hiccup for {doc_id}")
        doc = self._rs.get(doc_id)
        if doc_id in self._empty_for:
            doc.body = None
        return doc


def _default_payloads(docs: dict) -> dict:
    return {
        docs["doc1"].doc_id: {
            "claims": [
                {"text": "Acme shipped Orbital Billing 2.0.",
                 "quote": "Acme Ships Orbital Billing 2.0."},
                # A hallucinated citation is preserved verbatim; a missing
                # doc_id falls back to the batch doc_id (parse_claims rules).
                {"text": "Acme raised a Series F.", "doc_id": "doc-hallucinated"},
                {"text": "Orbital Billing is now GA."},
            ]
        },
        # A well-formed EMPTY extraction is a success — cached forever.
        docs["doc2"].doc_id: {"claims": []},
        docs["doc4"].doc_id: {
            "claims": [{"text": "Beta Labs launched a retry planner.",
                        "doc_id": docs["doc4"].doc_id,
                        "quote": "launches a retry planner"}]
        },
    }


@pytest.fixture
def lab_env(monkeypatch):
    """Consent + key present: the same egress posture the intel pipeline demands."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-lab-test")
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", DATA_EXIT_CONSENT)


def _run(tmp_path, db, rs, dossiers, fake, *, max_docs=10, budget_calls=10,
         domains=None, out_dir=None):
    return backfill(
        db, rs, domains,
        max_docs=max_docs,
        budget_calls=budget_calls,
        out_dir=out_dir or (tmp_path / "claims"),
        dossiers_dir=dossiers,
    )


# ------------------------------------------------------------ the happy path


def test_backfills_missing_claims_and_writes_cache(tmp_path, monkeypatch, lab_env):
    db, rs, dossiers, docs = _make_world(tmp_path)
    fake = FakeImplementer(_default_payloads(docs))
    monkeypatch.setattr("src.llm.implement.call_implementer", fake)

    out_dir = tmp_path / "claims"
    stats = _run(tmp_path, db, rs, dossiers, fake, out_dir=out_dir)

    # doc3's claims are already persisted -> never a candidate; the bogus
    # unjoined gate row is invisible. Exactly the backlog, in corpus order.
    assert stats == {"considered": 3, "backfilled": 3, "cached": 0, "skipped": 0, "errors": 0}
    assert fake.doc_ids == [docs["doc1"].doc_id, docs["doc2"].doc_id, docs["doc4"].doc_id]

    # Model/base_url come from decide.yaml's implementers.bulk; the key comes
    # from the env; the timeout mirrors the pipeline's bulk default.
    bulk = ((Config().load_yaml("decide").get("implementers") or {}).get("bulk") or {})
    assert bulk.get("model") and bulk.get("base_url")
    for call in fake.calls:
        assert call["model"] == bulk["model"]
        assert call["base_url"] == bulk["base_url"]
        assert call["api_key"] == "or-lab-test"
        assert call["timeout_s"] == 60.0

    # The prompt is the CURRENT build_extraction_messages shape: the
    # post-fix-wave contract that forbids envelope/meta claims.
    for call in fake.calls:
        system = call["messages"][0]["content"]
        assert "response envelope" in system
        assert "business-relevant" in system
        assert [m["role"] for m in call["messages"]] == ["system", "user"]

    # The implementer sees the FULL body, not the corpus join's 9600-char head.
    doc1_user = fake.calls[0]["messages"][1]["content"]
    assert DOC1_TEXT in doc1_user
    assert len(doc1_user) > len(truncate(DOC1_TEXT, 9600))
    assert DOC4_TEXT in fake.calls[2]["messages"][1]["content"]

    # Cache files, one per doc, in the persisted-claims shape.
    cached_doc1 = json.loads((out_dir / f"{docs['doc1'].doc_id}.json").read_text(encoding="utf-8"))
    assert cached_doc1["doc_id"] == docs["doc1"].doc_id
    assert cached_doc1["model"] == bulk["model"]
    assert cached_doc1["generated_at"]
    assert cached_doc1["claims"] == [
        {"text": "Acme shipped Orbital Billing 2.0.",
         "quote_span": "Acme Ships Orbital Billing 2.0.",
         "doc_id": docs["doc1"].doc_id},
        {"text": "Acme raised a Series F.", "quote_span": None, "doc_id": "doc-hallucinated"},
        {"text": "Orbital Billing is now GA.", "quote_span": None, "doc_id": docs["doc1"].doc_id},
    ]
    cached_doc2 = json.loads((out_dir / f"{docs['doc2'].doc_id}.json").read_text(encoding="utf-8"))
    assert cached_doc2["claims"] == []
    cached_doc4 = json.loads((out_dir / f"{docs['doc4'].doc_id}.json").read_text(encoding="utf-8"))
    assert cached_doc4["claims"] == [
        {"text": "Beta Labs launched a retry planner.",
         "quote_span": "launches a retry planner",
         "doc_id": docs["doc4"].doc_id},
    ]
    # No cache file for the doc whose claims were already persisted.
    assert not (out_dir / f"{docs['doc3'].doc_id}.json").exists()


def test_cache_forever_second_invocation_is_free(tmp_path, monkeypatch, lab_env):
    db, rs, dossiers, docs = _make_world(tmp_path)
    fake = FakeImplementer(_default_payloads(docs))
    monkeypatch.setattr("src.llm.implement.call_implementer", fake)

    _run(tmp_path, db, rs, dossiers, fake)
    assert len(fake.calls) == 3

    stats = _run(tmp_path, db, rs, dossiers, fake)
    assert stats == {"considered": 3, "backfilled": 0, "cached": 3, "skipped": 0, "errors": 0}
    # No new spend — the cache is the state, even for the empty-claims doc.
    assert len(fake.calls) == 3


# ------------------------------------------------------------- the hard bounds


def test_max_docs_bounds_the_invocation(tmp_path, monkeypatch, lab_env):
    db, rs, dossiers, docs = _make_world(tmp_path)
    fake = FakeImplementer(_default_payloads(docs))
    monkeypatch.setattr("src.llm.implement.call_implementer", fake)

    out_dir = tmp_path / "claims"
    stats = _run(tmp_path, db, rs, dossiers, fake, max_docs=2, out_dir=out_dir)

    assert stats == {"considered": 2, "backfilled": 2, "cached": 0, "skipped": 0, "errors": 0}
    assert fake.doc_ids == [docs["doc1"].doc_id, docs["doc2"].doc_id]
    # The third candidate is deferred to a later invocation — untouched.
    assert not (out_dir / f"{docs['doc4'].doc_id}.json").exists()


def test_budget_calls_never_exceeded_mid_flight(tmp_path, monkeypatch, lab_env):
    db, rs, dossiers, docs = _make_world(tmp_path)
    fake = FakeImplementer(_default_payloads(docs))
    monkeypatch.setattr("src.llm.implement.call_implementer", fake)

    out_dir = tmp_path / "claims"
    stats = _run(tmp_path, db, rs, dossiers, fake, budget_calls=1, out_dir=out_dir)

    assert stats == {"considered": 3, "backfilled": 1, "cached": 0, "skipped": 2, "errors": 0}
    # The counter is checked BEFORE each call: exactly one call went out.
    assert fake.doc_ids == [docs["doc1"].doc_id]
    assert (out_dir / f"{docs['doc1'].doc_id}.json").exists()
    assert not (out_dir / f"{docs['doc2'].doc_id}.json").exists()
    assert not (out_dir / f"{docs['doc4'].doc_id}.json").exists()


def test_zero_budget_makes_no_calls(tmp_path, monkeypatch, lab_env):
    db, rs, dossiers, _docs = _make_world(tmp_path)
    fake = FakeImplementer(default={"claims": []})
    monkeypatch.setattr("src.llm.implement.call_implementer", fake)

    stats = _run(tmp_path, db, rs, dossiers, fake, budget_calls=0)
    assert stats == {"considered": 3, "backfilled": 0, "cached": 0, "skipped": 3, "errors": 0}
    assert fake.calls == []


def test_budget_exhaustion_still_serves_cache_hits(tmp_path, monkeypatch, lab_env):
    # A prior invocation cached doc1; with zero budget left the run still
    # counts the cache hit (no call needed) and skips only the uncached.
    db, rs, dossiers, docs = _make_world(tmp_path)
    fake = FakeImplementer(_default_payloads(docs))
    monkeypatch.setattr("src.llm.implement.call_implementer", fake)
    _run(tmp_path, db, rs, dossiers, fake, max_docs=1)

    stats = _run(tmp_path, db, rs, dossiers, fake, budget_calls=0)
    assert stats == {"considered": 3, "backfilled": 0, "cached": 1, "skipped": 2, "errors": 0}
    assert len(fake.calls) == 1  # only the first invocation's call


# ----------------------------------------------------- failure and skip paths


def test_failed_call_is_an_error_not_cached_and_retryable(tmp_path, monkeypatch, lab_env):
    db, rs, dossiers, docs = _make_world(tmp_path)
    failing = FakeImplementer(default=None)  # the never-raise seam's None
    monkeypatch.setattr("src.llm.implement.call_implementer", failing)
    out_dir = tmp_path / "claims"

    stats = _run(tmp_path, db, rs, dossiers, failing, out_dir=out_dir)
    assert stats == {"considered": 3, "backfilled": 0, "cached": 0, "skipped": 0, "errors": 3}
    # Nothing cached on failure: a later invocation can retry the spend.
    assert not list(out_dir.glob("*.json"))

    good = FakeImplementer(_default_payloads(docs))
    monkeypatch.setattr("src.llm.implement.call_implementer", good)
    stats2 = _run(tmp_path, db, rs, dossiers, good, out_dir=out_dir)
    assert stats2 == {"considered": 3, "backfilled": 3, "cached": 0, "skipped": 0, "errors": 0}


def test_raising_double_never_raises_past_a_doc(tmp_path, monkeypatch, lab_env):
    db, rs, dossiers, _docs = _make_world(tmp_path)
    boom = FakeImplementer(default=RuntimeError("implementer exploded"))
    monkeypatch.setattr("src.llm.implement.call_implementer", boom)

    stats = _run(tmp_path, db, rs, dossiers, boom)
    assert stats == {"considered": 3, "backfilled": 0, "cached": 0, "skipped": 0, "errors": 3}
    assert len(boom.calls) == 3  # every doc got its disposition; none escaped


def test_unreadable_bodies_are_skipped(tmp_path, monkeypatch, lab_env):
    # The corpus scan joins from the real files; the loop's own get() then
    # hits an unreadable store for doc2 (raises) and doc4 (body gone) —
    # the window the defensive skip guards. Neither ever reaches a call.
    db, rs, dossiers, docs = _make_world(tmp_path)
    flaky = FlakyStore(
        rs,
        raise_for=(docs["doc2"].doc_id,),
        empty_for=(docs["doc4"].doc_id,),
    )
    fake = FakeImplementer(_default_payloads(docs))
    monkeypatch.setattr("src.llm.implement.call_implementer", fake)

    stats = _run(tmp_path, db, flaky, dossiers, fake)
    assert stats == {"considered": 3, "backfilled": 1, "cached": 0, "skipped": 2, "errors": 0}
    assert fake.doc_ids == [docs["doc1"].doc_id]


# ------------------------------------------------------------ scoping/preflight


def test_domains_filter_scopes_candidates(tmp_path, monkeypatch, lab_env):
    db, rs, dossiers, docs = _make_world(tmp_path)
    fake = FakeImplementer(_default_payloads(docs))
    monkeypatch.setattr("src.llm.implement.call_implementer", fake)

    stats = _run(tmp_path, db, rs, dossiers, fake, domains=["beta.io"])
    assert stats == {"considered": 1, "backfilled": 1, "cached": 0, "skipped": 0, "errors": 0}
    assert fake.doc_ids == [docs["doc4"].doc_id]


def test_preflight_requires_key_and_consent_before_any_work(tmp_path, monkeypatch):
    db, rs, dossiers, _docs = _make_world(tmp_path)
    fake = FakeImplementer(default={"claims": []})
    monkeypatch.setattr("src.llm.implement.call_implementer", fake)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("SIGNALS_DECIDE_DATA_EXIT", raising=False)

    out_dir = tmp_path / "claims"
    with pytest.raises(ValueError) as exc:
        _run(tmp_path, db, rs, dossiers, fake, out_dir=out_dir)
    assert "OPENROUTER_API_KEY" in str(exc.value)
    assert fake.calls == []
    assert not out_dir.exists()

    # Consent present but WRONG string: same refusal.
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-lab-test")
    monkeypatch.setenv("SIGNALS_DECIDE_DATA_EXIT", "no-i-do-not")
    with pytest.raises(ValueError):
        _run(tmp_path, db, rs, dossiers, fake, out_dir=out_dir)
    assert fake.calls == []


def test_empty_corpus_returns_zero_stats(tmp_path, monkeypatch, lab_env):
    db = Database(tmp_path / "empty.db")
    rs = RawStore(db, tmp_path / "raw")
    empty_dossiers = tmp_path / "dossiers"
    empty_dossiers.mkdir()
    fake = FakeImplementer(default={"claims": []})
    monkeypatch.setattr("src.llm.implement.call_implementer", fake)

    stats = _run(tmp_path, db, rs, empty_dossiers, fake)
    assert stats == {"considered": 0, "backfilled": 0, "cached": 0, "skipped": 0, "errors": 0}
    assert fake.calls == []
