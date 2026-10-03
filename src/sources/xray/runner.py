# src/sources/xray/runner.py
"""X-ray runner — the ONLY I/O layer of the xray package.

Posture: X-ray is a robots-exception source. It is MANUAL and OPT-IN (a human
invokes the CLI), SELF-PACED (the pace budget below), and must NEVER be wired
into a scheduler or the cadence fanout — nothing here runs unattended or on a
timer. A handful of queries per invocation, on the operator's own cadence.

Engine: ``ddg_lite`` ONLY — GET ``https://lite.duckduckgo.com/lite/?q=...`` is
the ONLY probe-validated path (data/probe/XRAY_SERP_2026_10.md). Google is a
documented NO-GO on every transport including the browser tier (the 200 JS-gate
shell never renders anchors), so no engine parameter exists here and none is
wanted. The lite endpoint is STOCHASTIC on operator queries — it serves 202
anomaly challenges and 403 error-lites on some requests — hence the attempt
loop below. Bodies are BODY-validated, never status-validated
(``is_challenge`` decides; challenge bodies ship with 200 AND 202 alike).

Injected I/O (the runner never imports a transport and never reads a real
clock, and its body only ever calls the injected ``sleep`` — the stdlib
``time.sleep`` below is just the keyword default so production wiring can omit
it; test fakes pass a recorder):

- ``fetch(url) -> (status, body)`` — the transport callable (probe-verified tier).
- ``clock() -> ISO-UTC str`` — ledger stamps only.
- ``sleep(seconds)`` — the pacing budget. Knobs: ``pace_s`` sleeps before EVERY
  fetch (the anti-ban budget), ``attempt_pause_s`` sleeps between attempts
  after a challenge or a clean-empty body, ``max_attempts`` per query,
  ``max_queries`` total queries per run (checked before starting a new query;
  a query already in its attempt loop finishes).

Never-guess persistence rules:

- Strings arrive as ready specs (the CLI runs ``load_library``); the runner
  never loads the library. A string whose built query is empty is skipped and
  ledgered (status ``"empty"``, ``attempts: 0``) — never fetched with a mangled
  query.
- company/hiring/intent queries: one ``identity_candidates`` row per query via
  ``IdentityCandidateStore.upsert_candidate(name=<the query string>, kind="domain",
  source="xray")``. DEVIATION from ``normalize_entity`` (deliberate, documented):
  normalize_entity is for company NAMES — an operator query ("site:... "we're
  hiring" ...") is not a name and must not be mangled, so the verbatim query
  string is the review-row key. Zero CompanyHits -> NO row (never an empty
  candidate row). The library entry's exclude noise-words are NOT fed to
  ``extract_companies`` (wave-1 finding) — it always gets ``{"linkedin.com"}``.
- The cohort (dedupe + profile matching) is read once from the handed-in db:
  all account domains AND names root-normalized through
  ``src.identity.domains.root_domain`` (Nones dropped). A known-root company
  hit is dropped by extract_companies itself; the ledger still shows
  ``company_hits`` > 0 (pre-cohort evidence count) with
  ``candidates_queued == 0`` so an all-known run stays visible.
- people queries: ``ProfileHit.company`` is matched against the cohort by EXACT
  casefold match against account names and account domains' roots (conservative
  v1 — no fuzzy matching). Matched -> ONE ``contacts`` row: ``name=""`` (SERP
  text is not a verified identity), title/seniority/linkedin fields from the
  hit, ``person_key = stable_id(slug, domain)`` (the people.py call shape,
  slug-derived so it never collides with raw-slug keys). UNMATCHED profile hits
  are LEDGER-ONLY — never auto-accounts, never identity_candidates (pinned v1
  default).

Every query gets exactly one ledger row AFTER its outcome (persistence
failures ride the event as ``persist_error`` — the row is never lost);
counts are never swallowed — the report keeps degradation visible.
``contacts_written``/``profiles_unmatched`` count write attempts per query:
the same person hit by two strings in one run counts twice even though the
contacts upsert coalesces to one row (per-query rows stay accurate).
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from src.core.db import Database, IdentityCandidateStore
from src.core.models import Contact
from src.core.textutil import guess_seniority, stable_id
from src.identity.domains import root_domain
from src.sources.xray.hits import extract_companies, extract_profiles
from src.sources.xray.ledger import append_event
from src.sources.xray.serp import ParseError, is_challenge, parse_results
from src.sources.xray.strings import build_query, encode_query

__all__ = ["ENGINE", "run_xray"]

# The probe verdict constant (data/probe/XRAY_SERP_2026_10.md). The CLI reads
# this as its default engine. There is deliberately no google path: NO-GO.
ENGINE = "ddg_lite"

# Always-excluded host for company extraction — NOT the library's exclude
# noise-words (those shape the query's minus-group only; wave-1 finding).
_EXCLUDE = {"linkedin.com"}


def run_xray(
    *,
    strings: list[dict],
    fetch: Callable[[str], tuple[int, str]],
    db: Database,
    ledger_path: str | Path,
    clock: Callable[[], str],
    sleep: Callable[[float], None] = time.sleep,
    slots: dict | None = None,
    kinds: set[str] | None = None,
    pace_s: float = 2.0,
    max_attempts: int = 2,
    attempt_pause_s: float = 45.0,
    max_queries: int | None = None,
) -> dict:
    """Run the string list end-to-end; return the run report.

    Statuses (ledger + report): ``ok`` | ``challenge`` (stochastic block,
    attempts exhausted) | ``error`` (fetch raised; no retry — transport errors
    are not the stochastic shape) | ``empty`` (clean zero-anchor body, attempts
    exhausted). ``report["queries"]`` counts queries that actually fetched;
    skipped empty-query strings ledger as ``"empty"`` with ``attempts: 0`` and
    count toward ``report["empty"]`` only.
    """
    slots = slots or {}
    known, name_index, root_index = _cohort(db)
    store = IdentityCandidateStore(db)
    report: dict = {
        "queries": 0,
        "ok": 0,
        "challenge": 0,
        "error": 0,
        "empty": 0,
        "company_candidates": 0,
        "contacts_written": 0,
        "profiles_unmatched": 0,
        "errors": [],  # [{"query", "reason"}] — degradation stays visible
    }

    for spec in strings:
        if kinds is not None and spec.get("kind") not in kinds:
            continue
        query = build_query(spec, **slots)
        if not query.strip():
            # Skip-and-ledger: never fetch a mangled/empty query.
            append_event(
                ledger_path,
                {
                    "string_id": spec["id"],
                    "query": query,
                    "engine": ENGINE,
                    "status": "empty",
                    "attempts": 0,
                    "results": 0,
                    "profile_hits": 0,
                    "company_hits": 0,
                    "candidates_queued": 0,
                    "contacts_written": 0,
                    "profiles_unmatched": 0,
                },
                clock=clock,
            )
            report["empty"] += 1
            continue
        if max_queries is not None and report["queries"] >= max_queries:
            break  # cap reached: stop before starting a new query

        report["queries"] += 1
        url = encode_query(query, ENGINE)

        # --- attempt loop (body-validated, never status-validated) ---------
        attempts = 0
        status = "error"  # defensive init; every path below sets status
        marker: str | None = None
        error: str | None = None
        results: list[dict] = []
        while True:
            sleep(pace_s)  # before EVERY fetch — the anti-ban budget
            attempts += 1
            try:
                _http_status, body = fetch(url)  # status ignored by design
            except Exception as exc:
                status, error = "error", str(exc)
                break
            marker = is_challenge(body)
            if marker is not None:
                if attempts < max_attempts:
                    sleep(attempt_pause_s)  # stochastic challenge: back off, retry
                    continue
                status = "challenge"
                break
            try:
                results = parse_results(body, engine=ENGINE)
            except ParseError:
                # is_challenge already ran clean on this body, so this is the
                # clean zero-anchor shape: retry like a challenge attempt,
                # else record status "empty" (exact status, no conflation).
                if attempts < max_attempts:
                    sleep(attempt_pause_s)
                    continue
                status, results = "empty", []
                break
            status = "ok"
            break

        # --- persistence (never-guess) -------------------------------------
        # Guarded: a persistence failure (locked sqlite, store error) must not
        # cost the query its ledger row — the one-row-per-query contract holds
        # for EVERY fetched query; the failure rides the event + report.
        n_rows = 0
        n_contacts = 0
        n_unmatched = 0
        n_profile_hits = 0
        n_company_hits = 0
        persist_error: str | None = None
        if status == "ok":
            try:
                string_id = spec["id"]
                if spec.get("kind") == "people":
                    profiles = extract_profiles(results, string_id=string_id)
                    n_profile_hits = len(profiles)
                    for hit in profiles:
                        account = _match_account(hit.company, name_index, root_index)
                        if account is None:
                            n_unmatched += 1  # ledger-only; never auto-persisted
                            continue
                        domain = account["domain"]
                        contact = Contact(
                            person_key=stable_id(hit.slug, domain),
                            domain=domain,
                            name="",  # SERP text is not a verified identity
                            title=hit.title,
                            seniority=guess_seniority(hit.title),
                            linkedin_slug=hit.slug,
                            linkedin_url=hit.url,
                        )
                        db.upsert("contacts", contact.to_db_row(), pk="person_key")
                        n_contacts += 1
                else:
                    # Evidence count BEFORE the cohort dedupe: a run whose company
                    # hits were all already-known accounts still shows hits > 0
                    # with candidates_queued == 0 (degradation stays visible).
                    n_company_hits = len(
                        extract_companies(
                            results, string_id=string_id,
                            known_domains=set(), exclude=_EXCLUDE,
                        )
                    )
                    companies = extract_companies(
                        results, string_id=string_id,
                        known_domains=known, exclude=_EXCLUDE,
                    )
                    if companies:
                        # ONE row per query; the verbatim query string is the
                        # review-row key (an operator query is not a company name).
                        store.upsert_candidate(
                            name=query,
                            kind="domain",
                            candidates=[
                                {
                                    "domain": h.domain,
                                    "url": h.url,
                                    "title": h.serp_title,
                                    "snippet": h.snippet,
                                    "string_id": h.string_id,
                                }
                                for h in companies
                            ],
                            source="xray",
                        )
                        n_rows = 1
            except Exception as exc:
                persist_error = str(exc)
                report["errors"].append(
                    {"query": query, "reason": f"persistence: {persist_error}"}
                )

        # --- ledger (one row per query, after its outcome) ------------------
        event = {
            "string_id": spec["id"],
            "query": query,
            "engine": ENGINE,
            "status": status,
            "attempts": attempts,
            "results": len(results),
            "profile_hits": n_profile_hits,
            "company_hits": n_company_hits,
            "candidates_queued": n_rows,
            "contacts_written": n_contacts,
            "profiles_unmatched": n_unmatched,
        }
        if status == "challenge":
            event["marker"] = marker
        if status == "error":
            event["error"] = error
        if persist_error:
            event["persist_error"] = persist_error
        append_event(ledger_path, event, clock=clock)

        report[status] += 1
        report["company_candidates"] += n_rows
        report["contacts_written"] += n_contacts
        report["profiles_unmatched"] += n_unmatched
        if status == "error":
            report["errors"].append({"query": query, "reason": error})

    return report


def _cohort(db: Database) -> tuple[set[str], dict[str, dict], dict[str, dict]]:
    """ONE accounts read -> (known root domains, name index, root index).

    ``known`` feeds extract_companies' cohort dedupe: every account domain AND
    name root-normalized through root_domain, Nones dropped (names like
    "Acme Corp" have no registrable root and fall out — never-guess).
    The indexes feed profile matching: names casefolded EXACTLY plus account
    domains' registrable roots (conservative v1, no fuzzy matching).
    """
    known: set[str] = set()
    name_index: dict[str, dict] = {}
    root_index: dict[str, dict] = {}
    for row in db.query("SELECT domain, name FROM accounts"):
        domain = (row.get("domain") or "").strip()
        name = (row.get("name") or "").strip()
        droot = root_domain(domain) if domain else None
        if droot:
            known.add(droot)
            root_index[droot] = {"domain": domain, "name": name}
        nroot = root_domain(name) if name else None
        if nroot:
            known.add(nroot)
        if name:
            name_index[name.casefold()] = {"domain": domain, "name": name}
    return known, name_index, root_index


def _match_account(
    company: str, name_index: dict[str, dict], root_index: dict[str, dict]
) -> dict | None:
    """Exact casefold match of a parsed company against the cohort, or None."""
    key = (company or "").strip().casefold()
    if not key:
        return None
    if key in name_index:
        return name_index[key]
    return root_index.get(key)  # root_domain keys are already casefolded
