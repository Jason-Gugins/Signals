"""Task 3 (decide-lab): supervised target builders — labels from the corpus.

Spec: docs/plans/2026-10-11-autoresearch-decide-lab.md (Task 3). Both
builders consume the Task 1 extractor's flat corpus rows and return
``(labeled_rows, stats)``, grouped by domain throughout (the later splits
are domain-grouped, so every stat carries its domain):

- ``claim_survival`` — row-level: one label per citation_soundness claim row
  (G4 writes one ledger row per judged claim; the batch ``claims`` payload
  rides every row). accepted -> 1, reviewed/dropped -> 0. Rows without
  persisted claims (the pre-``lab.persist_claims`` history) and rows with no
  verdict (``errored``) are skipped and counted, never silently dropped.
- ``doc_useful`` — doc-level: did a joined document_gate doc yield >=1
  accepted claim, joined via the citation rows' ``claims[].doc_id``. Docs
  whose only citation rows are errored carry no verdict signal and are
  skipped too.

Fixtures are synthetic flat rows in the extractor's exact output shape (the
tests/test_lab_corpus.py habit one level up — no DB/RawStore/world needed).
The vocabulary test pins the module's outcome constants to the REAL ledger
strings in src/decide/gates.py: the plan draft said kept/reviewed/dropped,
but the ledger writes accepted/reviewed/dropped (+ errored).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.lab.targets import (
    OUTCOME_ACCEPTED,
    OUTCOME_DROPPED,
    OUTCOME_ERRORED,
    OUTCOME_REVIEWED,
    claim_survival,
    doc_useful,
)

REPO = Path(__file__).resolve().parents[1]

RUN_A = "aaa11101"
RUN_B = "bbb22202"
RUN_C = "ccc33303"
DOMAIN_A = "acme.com"
DOMAIN_B = "beta.io"

DOC1, DOC2, DOC3 = "d1" * 32, "d2" * 32, "d3" * 32


def _pkg(domain: str, run_id: str) -> str:
    return f"{domain}-20261012T000000000000Z-{run_id}"


def _claim(doc_id: str, text: str = "A concrete claim.", quote_span: str = "A concrete") -> dict:
    """A persisted claim payload (the lab.persist_claims ledger field)."""
    return {"text": text, "quote_span": quote_span, "doc_id": doc_id}


def _cite_row(domain: str, run_id: str, outcome: str | None, *, reason: str | None = None,
              claims: list[dict] | None = None) -> dict:
    """A citation_soundness corpus row (the extractor's flat output shape).
    ``claims`` omitted entirely models the pre-persist_claims history."""
    row = {
        "row_type": "decision",
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
        "reason": reason,
        "package": _pkg(domain, run_id),
        "domain": domain,
        "market_profile_id": "ab_testing_profile",
        "generated_at": "2026-10-12T00:01:02+00:00",
        "account": domain.split(".")[0].capitalize(),
    }
    if claims:
        row["claims"] = claims
    return row


def _gate_row(domain: str, run_id: str, doc_id: str | None, *, joined: bool = True,
              outcome: str = "included", reason: str | None = None) -> dict:
    """A document_gate corpus row; ``joined=False`` models a state_hash that
    no longer matches any stored doc (doc_id None)."""
    row = {
        "row_type": "decision",
        "gate": "document_gate",
        "boundary": "document_gate",
        "state_hash": "cafe" * 16,
        "answers": {"is_relevant": {"type": "noul", "noul": 0.9}},
        "deterministic_action": "include",
        "agree": None,
        "agree_direction": None,
        "error": None,
        "latency_ms": 1220.054,
        "raw_tokens": 3821,
        "model": "jev-1.13.0",
        "run_id": run_id,
        "called": True,
        "outcome": outcome,
        "reason": reason,
        "package": _pkg(domain, run_id),
        "domain": domain,
        "market_profile_id": "ab_testing_profile",
        "generated_at": "2026-10-12T00:01:02+00:00",
        "account": domain.split(".")[0].capitalize(),
    }
    if joined:
        row.update({
            "doc_id": doc_id,
            "doc_joined": True,
            "text": "Acme Ships Orbital Billing 2.0.",
            "url": f"https://{domain}/blog/orbital-billing",
            "source": "blog",
            "byte_size": 34,
            "fetched_at": "2026-10-11T00:00:00+00:00",
        })
    else:
        row["doc_id"] = None
        row["doc_joined"] = False
    return row


def _evidence_row(domain: str) -> dict:
    return {
        "row_type": "evidence",
        "evidence_id": "ev-0001",
        "kind": "signal",
        "observed_at": "2026-10-12",
        "signal_id": "c1605c6b00d460c8",
        "source": "jobsignals",
        "url": None,
        "detail": "7 new Commercial Sales roles posted",
        "package": _pkg(domain, RUN_A),
        "domain": domain,
        "market_profile_id": "ab_testing_profile",
        "generated_at": "2026-10-12T00:01:02+00:00",
        "run_id": RUN_A,
        "account": domain.split(".")[0].capitalize(),
    }


# --- vocabulary ---------------------------------------------------------------


def test_outcome_vocabulary_matches_the_real_ledger_strings():
    """The constants ARE the gates.py ledger vocabulary — the plan draft's
    'kept' is not a ledger string and must never appear as one."""
    gates_src = (REPO / "src" / "decide" / "gates.py").read_text(encoding="utf-8")
    for outcome in (OUTCOME_ACCEPTED, OUTCOME_REVIEWED, OUTCOME_DROPPED, OUTCOME_ERRORED):
        assert f'"{outcome}"' in gates_src
    assert OUTCOME_ACCEPTED == "accepted"
    assert OUTCOME_REVIEWED == "reviewed"
    assert OUTCOME_DROPPED == "dropped"
    assert OUTCOME_ERRORED == "errored"
    assert '"kept"' not in gates_src


# --- claim_survival -------------------------------------------------------------


def test_claim_survival_labels_verdict_outcomes():
    rows = [
        _cite_row(DOMAIN_A, RUN_A, "accepted", claims=[_claim(DOC1, "c1", "q1")]),
        _cite_row(DOMAIN_A, RUN_A, "reviewed", reason="says_nothing", claims=[_claim(DOC1, "c2", "q2")]),
        _cite_row(DOMAIN_A, RUN_A, "dropped", reason="contradicted", claims=[_claim(DOC2, "c3", "q3")]),
        _cite_row(DOMAIN_B, RUN_C, "accepted", claims=[_claim(DOC3, "c4", "q4")]),
    ]
    labeled, stats = claim_survival(rows)
    assert [r["label"] for r in labeled] == [1, 0, 0, 1]
    assert all(r["target"] == "claim_survival" for r in labeled)
    # Labeled rows keep the full provenance for the downstream feature joins.
    assert labeled[0]["domain"] == DOMAIN_A
    assert labeled[0]["claims"] == [{"text": "c1", "quote_span": "q1", "doc_id": DOC1}]
    assert labeled[0]["noul"] == 0.91
    assert labeled[0]["run_id"] == RUN_A
    assert labeled[3]["domain"] == DOMAIN_B
    # Shallow copies: the source rows are untouched.
    assert all("label" not in r for r in rows)
    assert stats["target"] == "claim_survival"


def test_claim_survival_skips_rows_without_persisted_claims():
    rows = [
        _cite_row(DOMAIN_A, RUN_A, "accepted"),  # pre-persist_claims history: no claims key
        _cite_row(DOMAIN_A, RUN_A, "accepted", claims=[]),  # empty payload: equally unusable
        _cite_row(DOMAIN_B, RUN_C, "reviewed", reason="says_nothing"),  # skipped domain still reported
    ]
    labeled, stats = claim_survival(rows)
    assert labeled == []
    a = stats["per_domain"][DOMAIN_A]
    assert a["n"] == 0 and a["skipped"] == 2 and a["skipped_no_claims"] == 2
    b = stats["per_domain"][DOMAIN_B]
    assert b["n"] == 0 and b["skipped"] == 1 and b["skipped_no_claims"] == 1
    assert stats["overall"]["skipped"] == 3 and stats["overall"]["n"] == 0


def test_claim_survival_skips_rows_without_a_verdict():
    """errored rows carry no verdict signal (an outage is not a negative);
    unknown/missing outcomes are outside the vocabulary."""
    rows = [
        _cite_row(DOMAIN_A, RUN_A, "errored", claims=[_claim(DOC1)]),
        _cite_row(DOMAIN_A, RUN_A, "kept", claims=[_claim(DOC1)]),  # the plan draft's word: not real
        _cite_row(DOMAIN_A, RUN_A, "accepted", claims=[_claim(DOC2)]),
    ]
    no_outcome = _cite_row(DOMAIN_A, RUN_A, "reviewed", reason="says_nothing", claims=[_claim(DOC3)])
    del no_outcome["outcome"]
    rows.append(no_outcome)
    labeled, stats = claim_survival(rows)
    assert [r["label"] for r in labeled] == [1]
    a = stats["per_domain"][DOMAIN_A]
    assert a["skipped_outcome"] == 3 and a["skipped"] == 3
    assert a["n"] == 1 and a["positives"] == 1


def test_claim_survival_stats_per_domain_and_overall():
    rows = [
        _cite_row(DOMAIN_A, RUN_A, "accepted", claims=[_claim(DOC1)]),
        _cite_row(DOMAIN_A, RUN_A, "accepted", claims=[_claim(DOC1)]),
        _cite_row(DOMAIN_A, RUN_A, "reviewed", reason="says_nothing", claims=[_claim(DOC2)]),
        _cite_row(DOMAIN_A, RUN_A, "dropped", reason="prefilter", claims=[_claim(DOC2)]),
        _cite_row(DOMAIN_A, RUN_A, "reviewed", reason="output_review"),  # no claims: skipped
        _cite_row(DOMAIN_A, RUN_B, "errored", claims=[_claim(DOC2)]),  # no verdict: skipped
        _cite_row(DOMAIN_B, RUN_C, "accepted", claims=[_claim(DOC3)]),
    ]
    labeled, stats = claim_survival(rows)
    assert len(labeled) == 5
    a = stats["per_domain"][DOMAIN_A]
    assert a["n"] == 4 and a["positives"] == 2 and a["negatives"] == 2
    assert a["skipped"] == 2 and a["skipped_no_claims"] == 1 and a["skipped_outcome"] == 1
    assert a["positive_rate"] == 0.5
    b = stats["per_domain"][DOMAIN_B]
    assert b["n"] == 1 and b["positives"] == 1 and b["negatives"] == 0
    assert b["positive_rate"] == 1.0 and b["skipped"] == 0
    overall = stats["overall"]
    assert overall["n"] == 5 and overall["positives"] == 3 and overall["negatives"] == 2
    assert overall["skipped"] == 2 and overall["skipped_no_claims"] == 1 and overall["skipped_outcome"] == 1
    assert overall["positive_rate"] == 0.6


def test_claim_survival_review_reason_distribution():
    """G4's wall history rides in the stats: the per-domain review-reason
    distribution (says_nothing / output_review / review_label_prob)."""
    rows = [
        _cite_row(DOMAIN_A, RUN_A, "reviewed", reason="says_nothing", claims=[_claim(DOC1)]),
        _cite_row(DOMAIN_A, RUN_A, "reviewed", reason="says_nothing", claims=[_claim(DOC1)]),
        _cite_row(DOMAIN_A, RUN_A, "reviewed", reason="review_label_prob", claims=[_claim(DOC2)]),
        _cite_row(DOMAIN_A, RUN_A, "reviewed", reason="output_review"),  # no claims: still context
        _cite_row(DOMAIN_B, RUN_C, "dropped", reason="contradicted", claims=[_claim(DOC3)]),
    ]
    _, stats = claim_survival(rows)
    assert stats["per_domain"][DOMAIN_A]["review_reasons"] == {
        "says_nothing": 2, "review_label_prob": 1, "output_review": 1,
    }
    assert stats["per_domain"][DOMAIN_B]["review_reasons"] == {}
    assert stats["overall"]["review_reasons"] == {
        "says_nothing": 2, "review_label_prob": 1, "output_review": 1,
    }


def test_claim_survival_ignores_non_citation_rows():
    rows = [
        _gate_row(DOMAIN_A, RUN_A, DOC1),
        {"gate": "routing", "boundary": "routing", "row_type": "decision",
         "run_id": RUN_A, "domain": DOMAIN_A, "outcome": None},
        _evidence_row(DOMAIN_A),
    ]
    labeled, stats = claim_survival(rows)
    assert labeled == []
    assert stats["overall"]["n"] == 0 and stats["overall"]["skipped"] == 0
    assert stats["per_domain"] == {}


# --- doc_useful -----------------------------------------------------------------


def test_doc_useful_labels_joined_docs_by_claim_join():
    rows = [
        _gate_row(DOMAIN_A, RUN_A, DOC1),
        _gate_row(DOMAIN_A, RUN_A, DOC2, outcome="excluded", reason="irrelevant"),
        _gate_row(DOMAIN_B, RUN_C, DOC3),
        _cite_row(DOMAIN_A, RUN_A, "accepted", claims=[_claim(DOC1)]),
        _cite_row(DOMAIN_A, RUN_A, "reviewed", reason="says_nothing", claims=[_claim(DOC2)]),
    ]
    labeled, stats = doc_useful(rows)
    by_doc = {r["doc_id"]: r["label"] for r in labeled}
    assert by_doc == {DOC1: 1, DOC2: 0}  # DOC2 shadow-excluded, still judged at G4
    assert all(r["target"] == "doc_useful" for r in labeled)
    # Shallow copies: the source rows are untouched.
    assert all("label" not in r for r in rows)
    a = stats["per_domain"][DOMAIN_A]
    assert a["n"] == 2 and a["positives"] == 1 and a["negatives"] == 1
    assert a["positive_rate"] == 0.5
    # DOC3 joined but never referenced by any claims-carrying citation row.
    b = stats["per_domain"][DOMAIN_B]
    assert b["n"] == 0 and b["skipped"] == 1 and b["skipped_no_claims"] == 1
    assert stats["overall"]["n"] == 2


def test_doc_useful_skips_unjoined_and_unreferenced_docs():
    rows = [
        _gate_row(DOMAIN_B, RUN_C, None, joined=False),  # hash no longer in the store
        _cite_row(DOMAIN_B, RUN_C, "accepted", claims=[_claim(DOC3)]),
        _cite_row(DOMAIN_B, RUN_C, "dropped", reason="prefilter",
                  claims=[_claim("f" * 64)]),  # a doc no gate row staged: no example either way
    ]
    labeled, stats = doc_useful(rows)
    assert labeled == []
    b = stats["per_domain"][DOMAIN_B]
    assert b["skipped_unjoined"] == 1 and b["skipped"] == 1 and b["n"] == 0
    assert stats["overall"]["skipped_unjoined"] == 1


def test_doc_useful_errored_only_docs_carry_no_label():
    """A doc whose only citation rows errored has no verdict signal —
    skipped (skipped_outcome), never labeled 0; a verdict row makes it a 0."""
    rows = [
        _gate_row(DOMAIN_A, RUN_A, DOC1),
        _gate_row(DOMAIN_A, RUN_A, DOC2),
        _cite_row(DOMAIN_A, RUN_A, "errored", claims=[_claim(DOC1)]),
        _cite_row(DOMAIN_A, RUN_A, "errored", claims=[_claim(DOC2)]),
        _cite_row(DOMAIN_A, RUN_A, "dropped", reason="prefilter", claims=[_claim(DOC2)]),
    ]
    labeled, stats = doc_useful(rows)
    assert {r["doc_id"]: r["label"] for r in labeled} == {DOC2: 0}
    a = stats["per_domain"][DOMAIN_A]
    assert a["skipped_outcome"] == 1 and a["skipped"] == 1
    assert a["n"] == 1 and a["negatives"] == 1


def test_doc_useful_same_doc_gated_across_runs_shares_the_doc_label():
    """The doc-level join crosses runs: one accepted claim anywhere labels
    every staged (joined) gate row for that doc."""
    rows = [
        _gate_row(DOMAIN_A, RUN_A, DOC1),
        _gate_row(DOMAIN_A, RUN_B, DOC1),
        _cite_row(DOMAIN_A, RUN_B, "accepted", claims=[_claim(DOC1)]),
    ]
    labeled, stats = doc_useful(rows)
    assert len(labeled) == 2
    assert {r["label"] for r in labeled} == {1}
    assert {r["run_id"] for r in labeled} == {RUN_A, RUN_B}
    assert stats["per_domain"][DOMAIN_A]["positives"] == 2


def test_doc_useful_class_balance_flags_sparse_positives():
    """Honesty: a <5% positive rate is flagged in the stats themselves."""
    rows = []
    for i in range(25):  # acme: 1 positive / 25 docs = 4%
        doc = f"a{i:062d}"
        rows.append(_gate_row(DOMAIN_A, RUN_A, doc))
        rows.append(_cite_row(DOMAIN_A, RUN_A, "accepted" if i == 0 else "dropped",
                              claims=[_claim(doc)]))
    for i, outcome in enumerate(("accepted", "dropped")):  # beta: 50%
        doc = f"b{i:062d}"
        rows.append(_gate_row(DOMAIN_B, RUN_C, doc))
        rows.append(_cite_row(DOMAIN_B, RUN_C, outcome, claims=[_claim(doc)]))
    labeled, stats = doc_useful(rows)
    assert len(labeled) == 27
    a = stats["per_domain"][DOMAIN_A]
    assert a["positive_rate"] == pytest.approx(0.04)
    assert a["positives_below_5pct"] is True
    b = stats["per_domain"][DOMAIN_B]
    assert b["positive_rate"] == 0.5 and b["positives_below_5pct"] is False
    # Overall 2/27 ≈ 7.4% — above the line, not flagged.
    assert stats["overall"]["positives"] == 2 and stats["overall"]["n"] == 27
    assert stats["overall"]["positive_rate"] == pytest.approx(2 / 27)
    assert stats["overall"]["positives_below_5pct"] is False


def test_doc_useful_carries_review_reason_context():
    rows = [
        _gate_row(DOMAIN_A, RUN_A, DOC1),
        _cite_row(DOMAIN_A, RUN_A, "reviewed", reason="output_review", claims=[_claim(DOC1)]),
    ]
    _, stats = doc_useful(rows)
    assert stats["per_domain"][DOMAIN_A]["review_reasons"] == {"output_review": 1}
    assert stats["overall"]["review_reasons"] == {"output_review": 1}


def test_doc_useful_ignores_non_gate_rows():
    rows = [
        _cite_row(DOMAIN_A, RUN_A, "accepted", claims=[_claim(DOC1)]),
        _evidence_row(DOMAIN_A),
    ]
    labeled, stats = doc_useful(rows)
    assert labeled == []
    assert stats["per_domain"] == {} and stats["overall"]["n"] == 0
