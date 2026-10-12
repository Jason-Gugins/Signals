"""Supervised target builders (Task 3) — labels from the ledger corpus.

Two targets over :func:`src.lab.corpus.extract` rows, both domain-grouped
(every label and stat carries its domain — the later splits are
domain-grouped, so the group key must survive label construction):

1. :func:`claim_survival` — row-level. G4 writes ONE ledger row per judged
   claim (the batch ``claims`` payload rides every row for diagnosis), so
   one ``citation_soundness`` row IS one claim's verdict:
   ``accepted`` -> 1, ``reviewed``/``dropped`` -> 0. Rows without persisted
   claims (the pre-``lab.persist_claims`` history — the overwhelming
   majority of today's ledgers) are skipped and counted, never silently
   dropped; rows whose outcome carries no verdict signal (``errored`` — the
   Jev call failed; an outage is not a negative) are skipped too.
2. :func:`doc_useful` — doc-level: did a joined ``document_gate`` document
   yield >=1 accepted claim? The join runs through the citation rows'
   persisted ``claims[].doc_id`` — the only doc link a citation row carries
   (each G4 batch is one document's claims, so a row's verdict marks that
   document). A document is labelable when >=1 of its citation rows carries
   a verdict outcome; documents whose citation rows are all ``errored``
   carry no verdict signal and are skipped, as are gate rows that no longer
   join to a stored doc and joined documents no citation row references.

Both return ``(labeled_rows, stats)``. ``stats`` carries, per domain AND
rolled into ``overall``: ``n`` / ``positives`` / ``negatives`` / ``skipped``
(with the ``skipped_no_claims`` / ``skipped_outcome`` / ``skipped_unjoined``
breakdowns — a target leaves its inapplicable counters at 0),
``positive_rate``, and ``positives_below_5pct`` — the class-balance honesty
flag (a doc-gate target under 5% positives cannot tune anything, and the
stats must say so). It also carries the ``review_reasons`` distribution per
domain (``says_nothing`` / ``output_review`` / ``review_label_prob`` /
``no_answer`` / ...): G4's wall history lives in that distribution
(data/probe/G4_WALL_2026_10.md — the pre-fix wave routed ~97% of claims to
reviewed, mostly ``says_nothing``), so every consumer of these labels sees
the wall it is tuning against. Reviewed rows contribute their reason even
when their claims were never persisted — the reason is on the row, not the
payload.

Outcome vocabulary: the REAL ledger strings from src/decide/gates.py
(``DecideLedger.record``'s aggregate set + ``gate_citation_batch``'s
routing). The plan draft said kept/reviewed/dropped; the authoritative
strings are accepted/reviewed/dropped (+ errored, which is skipped here —
it is not a verdict). Kept as module constants;
tests/test_lab_targets.py pins them to gates.py so the vocabulary cannot
drift.

Stdlib only, no pipeline imports (the scope guard:
src/lab never enters the intel import graph).
"""

from __future__ import annotations

# The authoritative citation-row outcome vocabulary, verbatim from
# src/decide/gates.py. The plan draft's "kept" does not exist in the ledger
# — "accepted" is the positive class.
OUTCOME_ACCEPTED = "accepted"
OUTCOME_REVIEWED = "reviewed"
OUTCOME_DROPPED = "dropped"
OUTCOME_ERRORED = "errored"

# Verdict outcomes -> label. Outcomes outside this map (errored, missing)
# carry no verdict signal and are skipped, never labeled.
_CLAIM_LABEL = {
    OUTCOME_ACCEPTED: 1,
    OUTCOME_REVIEWED: 0,
    OUTCOME_DROPPED: 0,
}
_VERDICT_OUTCOMES = frozenset(_CLAIM_LABEL)

_SPARSE_POSITIVE_RATE = 0.05  # the class-balance honesty line (5%)


def _domain_of(row: dict) -> str:
    domain = row.get("domain")
    return str(domain) if domain else "unknown"


def _review_reason_of(row: dict) -> str:
    reason = row.get("reason")
    return reason if isinstance(reason, str) and reason else "unspecified"


def _domain_stats() -> dict:
    return {
        "n": 0,
        "positives": 0,
        "negatives": 0,
        "skipped": 0,
        # Breakdowns (each target leaves its inapplicable counters at 0):
        "skipped_no_claims": 0,  # no persisted claims to attribute a verdict to
        "skipped_outcome": 0,  # claims exist but no verdict outcome (errored)
        "skipped_unjoined": 0,  # doc-level only: gate row no longer joins a stored doc
        "positive_rate": None,
        "positives_below_5pct": False,
        "review_reasons": {},
    }


def _new_stats(target: str) -> dict:
    return {"target": target, "per_domain": {}, "overall": None}


def _stats_for(stats: dict, domain: str) -> dict:
    return stats["per_domain"].setdefault(domain, _domain_stats())


def _skip(stats: dict, domain: str, breakdown: str) -> None:
    ds = _stats_for(stats, domain)
    ds["skipped"] += 1
    ds[breakdown] += 1


def _count_label(ds: dict, label: int) -> None:
    ds["n"] += 1
    ds["positives" if label == 1 else "negatives"] += 1


def _count_review_reasons(rows: list[dict], stats: dict) -> None:
    """The per-domain review-reason distribution (G4's wall history:
    says_nothing / output_review / review_label_prob / no_answer)."""
    for row in rows:
        if row.get("gate") != "citation_soundness":
            continue
        if row.get("outcome") != OUTCOME_REVIEWED:
            continue
        ds = _stats_for(stats, _domain_of(row))
        reason = _review_reason_of(row)
        ds["review_reasons"][reason] = ds["review_reasons"].get(reason, 0) + 1


def _with_class_balance(ds: dict) -> None:
    ds["positive_rate"] = (ds["positives"] / ds["n"]) if ds["n"] else None
    rate = ds["positive_rate"]
    ds["positives_below_5pct"] = rate is not None and rate < _SPARSE_POSITIVE_RATE


def _finalize(stats: dict) -> None:
    """Derive each domain's class balance, then roll everything into
    ``overall`` (domains in name order for deterministic reports)."""
    overall = _domain_stats()
    for domain in sorted(stats["per_domain"]):
        ds = stats["per_domain"][domain]
        for key in (
            "n", "positives", "negatives", "skipped",
            "skipped_no_claims", "skipped_outcome", "skipped_unjoined",
        ):
            overall[key] += ds[key]
        for reason, count in ds["review_reasons"].items():
            overall["review_reasons"][reason] = overall["review_reasons"].get(reason, 0) + count
        _with_class_balance(ds)
    _with_class_balance(overall)
    stats["overall"] = overall


def claim_survival(rows: list[dict]) -> tuple[list[dict], dict]:
    """Row-level target: one label per ``citation_soundness`` claim row.

    Each labeled row is a shallow copy of its corpus row plus
    ``target``/``label`` — the full provenance (domain, package, run_id,
    claims payload, noul, reason, raw_tokens, latency_ms) rides along for
    the downstream feature joins, and the source rows are never mutated.
    A row is labeled only when claims were persisted AND its outcome is a
    verdict (accepted/reviewed/dropped); everything else is skipped and
    counted per domain, with its reason (no claims vs no verdict).
    """
    stats = _new_stats("claim_survival")
    labeled: list[dict] = []
    for row in rows:
        if row.get("gate") != "citation_soundness":
            continue
        domain = _domain_of(row)
        claims = row.get("claims")
        if not isinstance(claims, list) or not claims:
            _skip(stats, domain, "skipped_no_claims")
            continue
        label = _CLAIM_LABEL.get(row.get("outcome"))
        if label is None:
            _skip(stats, domain, "skipped_outcome")
            continue
        out = dict(row)
        out["target"] = "claim_survival"
        out["label"] = label
        labeled.append(out)
        _count_label(_stats_for(stats, domain), label)
    _count_review_reasons(rows, stats)
    _finalize(stats)
    return labeled, stats


def doc_useful(rows: list[dict]) -> tuple[list[dict], dict]:
    """Doc-level target: did a joined ``document_gate`` document yield >=1
    accepted claim?

    The document join runs through the citation rows' persisted
    ``claims[].doc_id``. One labeled row per JOINED gate row (a staged-doc
    event — a document gated in two runs labels twice, both from the same
    doc-level join; the domain-grouped splits keep such duplicates inside
    one fold). A document is labelable when >=1 of its citation rows
    carries a verdict outcome; documents only referenced by errored rows
    (``skipped_outcome``), never referenced at all (``skipped_no_claims``),
    or whose gate row no longer joins a stored doc (``skipped_unjoined``)
    are skipped and counted per domain.

    Labeled rows are shallow copies plus ``target``/``label``; the gate
    row's answers/text/cost fields ride along for the doc-gate features.
    """
    # Pass 1 over the citation rows: which documents were referenced, which
    # carry a verdict, and which yielded >=1 accepted claim.
    accepted_docs: set[str] = set()
    verdict_docs: set[str] = set()
    referenced_docs: set[str] = set()
    for row in rows:
        if row.get("gate") != "citation_soundness":
            continue
        claims = row.get("claims")
        if not isinstance(claims, list):
            continue
        doc_ids = {
            str(c["doc_id"])
            for c in claims
            if isinstance(c, dict) and c.get("doc_id")
        }
        if not doc_ids:
            continue
        referenced_docs.update(doc_ids)
        outcome = row.get("outcome")
        if outcome in _VERDICT_OUTCOMES:
            verdict_docs.update(doc_ids)
            if outcome == OUTCOME_ACCEPTED:
                accepted_docs.update(doc_ids)

    # Pass 2 over the gate rows: label every joined, verdict-backed doc.
    stats = _new_stats("doc_useful")
    labeled: list[dict] = []
    for row in rows:
        if row.get("gate") != "document_gate":
            continue
        domain = _domain_of(row)
        if not row.get("doc_joined") or not row.get("doc_id"):
            _skip(stats, domain, "skipped_unjoined")
            continue
        doc_id = str(row["doc_id"])
        if doc_id not in verdict_docs:
            breakdown = "skipped_outcome" if doc_id in referenced_docs else "skipped_no_claims"
            _skip(stats, domain, breakdown)
            continue
        label = 1 if doc_id in accepted_docs else 0
        out = dict(row)
        out["target"] = "doc_useful"
        out["label"] = label
        labeled.append(out)
        _count_label(_stats_for(stats, domain), label)
    _count_review_reasons(rows, stats)
    _finalize(stats)
    return labeled, stats
