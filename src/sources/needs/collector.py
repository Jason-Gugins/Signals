"""Profile-matched needs: first-party need statements + explicit stack demands.

Two evidence sets feed this source, and both are *explicit*:

  * Part 1 promotes first-party ``company_feed`` sentences that say the
    company is building / rolling out / standing up / migrating /
    consolidating / expanding / hiring (see
    :mod:`src.sources.needs.extract`). The verbatim sentence is the evidence.
  * Part 2 promotes open job postings that explicitly require a vendor, via
    the jobsignals required-stack extractor. The verbatim phrase is the
    evidence.

A candidate is emitted only when :func:`match_relevance` says the observation
matches the seller's selected market profile. With no raw store or no market
profile there is no relevance vocabulary, so nothing is promoted -- never
everything as a fallback.

The source never reads the ``technologies`` table and never claims absence.
The claim is only "this quote says X and X is in the seller's served set" or
"this job explicitly demands Y and Y is in the seller's served set"; an
unobserved tool is simply not emitted. No network, no clock beyond the
passed-in ``today``, no filesystem access except through the injected
``raw_store``.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from src.core.models import Account
from src.core.textutil import sha256_hex
from src.intel.market import match_relevance
from src.sources.base import SignalCandidate, SourceAdapter
from src.sources.jobsignals.analyze import (
    _vendor_vocab,
    extract_required_stack_demands,
)
from src.sources.needs.extract import extract_need_statements
from src.sources.news.feeds import html_to_text
from src.sources.registry import register

#: Title truncation, shared by both parts.
_TITLE_CHARS = 160

#: Natural-key hash length for need statements.
_KEY_HASH_CHARS = 12

#: Confidence stamped on every promoted observation.
_CONFIDENCE = 0.7

#: Undated documents rank oldest, so a timestamped copy always wins.
_EPOCH_MIN = datetime.min.replace(tzinfo=timezone.utc)


def _normalise_quote(quote: str) -> str:
    """Whitespace-collapse a quote: the identity of a need claim.

    Storage renders the same sentence with different dynamic bytes (markup
    wrappers, trailing whitespace), so whitespace-only differences must not
    read as different claims.
    """
    return " ".join(str(quote or "").split())


def _fetched_at_rank(doc) -> datetime:
    """Sort key: ``fetched_at`` as an aware UTC datetime; never raises.

    Unparsable or absent timestamps rank at datetime.min so a dated copy
    always wins over an undated one.
    """
    raw = str(getattr(doc, "fetched_at", None) or "").strip()
    if not raw:
        return _EPOCH_MIN
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return _EPOCH_MIN
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _normalise_vocab(vocab) -> list[tuple[str, re.Pattern]]:
    """Accept vocab pairs ("name", compiled) or bare strings ("name")."""
    pairs: list[tuple[str, re.Pattern]] = []
    for item in vocab or ():
        if isinstance(item, (tuple, list)) and len(item) == 2:
            name, pattern = item
            pairs.append((str(name), pattern))
        else:
            name = str(item).casefold()
            pairs.append((name, re.compile(rf"\b{re.escape(name)}\b", re.I)))
    return pairs


def _decode(body) -> str:
    """Body bytes -> text with a replacement policy; never raises."""
    if body is None:
        return ""
    if isinstance(body, (bytes, bytearray)):
        return bytes(body).decode("utf-8", errors="replace")
    return str(body)


def _first_party_doc_ids(db, domain: str) -> list[str]:
    """Doc ids for first-party text this account's company_feed fetched.

    documents.source records only the FIRST writer of a content hash, so a
    feed that feed_discovery fetched seconds earlier is stored under that
    label and iter_docs(source="company_feed") never sees it (live case:
    darktrace.com 2026-09-15 - sha256(body) == doc_id, source='feed_discovery').
    fetch_log keeps the (source, domain, url) provenance, so resolve URLs from
    there and map them to documents by URL.

    The company_feed-labelled rows are UNIONed in because prune deletes
    fetch_log rows past --keep-days: provenance must not become the only way
    to see a legitimately-labelled document. documents.url has no index
    (src/core/db.py:171-172), so the exact domain-scoped match runs first and
    the normalised compare is only a fallback for trailing-slash variants.
    """
    out: list[str] = []
    seen: set[str] = set()

    def _add(doc_id: str | None) -> None:
        if doc_id and doc_id not in seen:
            seen.add(doc_id)
            out.append(doc_id)

    for row in db.query(
        "SELECT doc_id FROM documents WHERE source='company_feed' AND domain=?", (domain,)
    ):
        _add(row.get("doc_id"))
    for row in db.query(
        "SELECT url FROM fetch_log WHERE source='company_feed' AND domain=? "
        "AND status >= 200 AND status < 300 ORDER BY at DESC",
        (domain,),
    ):
        url = (row.get("url") or "").strip()
        if not url:
            continue
        stripped = url.rstrip("/")
        doc = db.one(
            "SELECT doc_id FROM documents WHERE domain=? AND (url=? OR url=? OR url=?)",
            (domain, url, stripped, stripped + "/"),
        ) or db.one(
            "SELECT doc_id FROM documents WHERE domain=? AND rtrim(url,'/') = rtrim(?,'/')",
            (domain, url),
        )
        _add(doc.get("doc_id") if doc else None)
    return out


@register
class NeedsSource(SourceAdapter):
    key = "needs"
    tier = "local"
    cadence_hours = 24
    emits = ("need_statement", "required_stack_demand")

    def plan(self, account: Account, cursor):
        return []

    def parse(self, doc, account, task_meta):
        return []

    def local_harvest(self, *, db, account, today, task_meta):
        meta = task_meta or {}
        raw_store = meta.get("raw_store")
        profile = meta.get("market_profile")
        # No relevance vocabulary means nothing is promoted. Never promote
        # everything as a fallback.
        if raw_store is None or profile is None:
            return []

        vocab = meta.get("vendor_vocab")
        vocab = _normalise_vocab(vocab) if vocab is not None else _vendor_vocab()

        observed_at = today.isoformat()
        out: list[SignalCandidate] = []
        out.extend(self._need_candidates(raw_store, account, profile, observed_at, db))
        out.extend(self._demand_candidates(db, account, profile, vocab, observed_at))
        return out

    # -- Part 1: need statements from first-party documents ---------------

    def _need_candidates(self, raw_store, account, profile, observed_at, db):
        """Bodies are rendered to text before extraction (html_to_text is
        idempotent on plain text), then loaded for provenance-resolved doc ids.

        Metadata-only iter_docs is no longer used here: documents.source records
        only the first writer of a content hash, so a first-party feed labelled
        'feed_discovery' would otherwise be invisible. doc ids come from
        _first_party_doc_ids(), which resolves them from fetch_log provenance
        UNIONed with the company_feed label.

        Dedupe rule: at most ONE candidate per distinct NORMALISED quote text
        (``" ".join(quote.split())``). Content-addressed storage mints a new
        document row whenever the body bytes differ at all, so one boilerplate
        sentence can be stored N times; promoting per document turned that
        into N identical signals (the live darktrace.com run: 15 signals for
        one sentence). The winner -- the newest ``fetched_at``, ties broken by
        the lexicographically LARGEST ``doc_id`` -- keeps its doc_id/url/source
        in evidence, so every signal still cites real provenance.
        """
        winner_of: dict[str, tuple[tuple, SignalCandidate]] = {}
        order: list[str] = []
        for doc_id in _first_party_doc_ids(db, account.domain):
            try:
                doc = raw_store.get(doc_id)
            except Exception:
                continue  # unreadable body: skip, never raise
            if doc is None:
                continue
            text = html_to_text(_decode(getattr(doc, "body", None)))
            if not text.strip():
                continue
            for row in extract_need_statements(text):
                quote = row.get("quote") or ""
                if not quote:
                    continue
                normalised = _normalise_quote(quote)
                if not normalised:
                    continue
                match = match_relevance(
                    text=quote,
                    department=None,
                    signal_type="need_statement",
                    vendors=[],
                    profile=profile,
                )
                if match is None:
                    continue
                rank = (_fetched_at_rank(doc), str(getattr(doc, "doc_id", "") or ""))
                current = winner_of.get(normalised)
                if current is not None and current[0] >= rank:
                    continue
                evidence = {
                    "quote": quote,
                    "matched_phrase": row.get("matched_phrase"),
                    "doc_id": doc.doc_id,
                    "url": getattr(doc, "url", None),
                    "source": getattr(doc, "source", None),
                    "offering_id": match.offering_id,
                    "reasons": list(match.reasons),
                }
                fetched_at = getattr(doc, "fetched_at", None)
                if fetched_at:
                    evidence["fetched_at"] = fetched_at
                # The quote is the CLAIM; the document is only its PROVENANCE.
                # doc_id is therefore NOT part of the natural key: a re-stored
                # copy of the same sentence is the same signal, so re-runs are
                # idempotent and a later copy never mints a second signal.
                candidate = SignalCandidate(
                    "need_statement",
                    observed_at,
                    f"need:{account.domain}:"
                    f"{sha256_hex(normalised)[:_KEY_HASH_CHARS]}",
                    title=quote[:_TITLE_CHARS],
                    confidence=_CONFIDENCE,
                    evidence_data=evidence,
                )
                winner_of[normalised] = (rank, candidate)
                if current is None:
                    order.append(normalised)
        return [winner_of[key][1] for key in order]

    # -- Part 2: explicit required-stack demands from open jobs -----------

    def _demand_candidates(self, db, account, profile, vocab, observed_at):
        rows = db.query(
            """SELECT job_key, title, department, url, description FROM jobs
               WHERE domain=? AND closed_at IS NULL""",
            (account.domain,),
        )
        out: list[SignalCandidate] = []
        for row in rows:
            description = row.get("description") or ""
            if not description.strip():
                continue
            for demand in extract_required_stack_demands(description, vocab):
                vendor = demand.get("vendor")
                if not vendor:
                    continue
                match = match_relevance(
                    text=description,
                    department=row.get("department"),
                    signal_type="required_stack_demand",
                    vendors=[vendor],
                    profile=profile,
                )
                if match is None:
                    continue
                title = row.get("title") or vendor
                out.append(
                    SignalCandidate(
                        "required_stack_demand",
                        observed_at,
                        f"stack-demand:{account.domain}:{row.get('job_key')}:{vendor}",
                        title=str(title)[:_TITLE_CHARS],
                        confidence=_CONFIDENCE,
                        evidence_data={
                            "job_key": row.get("job_key"),
                            "job_url": row.get("url"),
                            "phrase": demand.get("phrase"),
                            "vendor": vendor,
                            "department": row.get("department"),
                            "offering_id": match.offering_id,
                            "reasons": list(match.reasons),
                            "title": row.get("title"),
                        },
                    )
                )
        return out
