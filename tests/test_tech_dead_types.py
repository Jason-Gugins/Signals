"""Wiring tests for the unreachable signal types (plan Task 4).

competitor_detected / tech_removed / backfill_open are registered in the
taxonomy but had no live production call site. These tests pin each wiring:

- 4a: techstack parse threads config/fingerprints.yaml ``competitors`` into
  ``tech_to_candidates`` so a named-vendor match emits competitor_detected.
- 4b: the runner captures ``upsert_technologies``'s (new, gone) return and
  persists tech_removed for confirmed removals (missing_runs >= 2).
- 4c: jobsignals local_harvest emits backfill_open when a recently-closed
  title is open again (casefold match, 120d window, monthly natural key).
"""

from __future__ import annotations

from datetime import date

from src.core.models import Account, Document
from src.sources.techstack.collector import TechstackSource, tech_to_candidates

TODAY = date(2026, 9, 4)


# ── 4a: competitor_detected ─────────────────────────────────────────────────


def test_tech_to_candidates_emits_competitor_detected():
    cands = tech_to_candidates(
        "acme.com", ["hubspot"], [], [], {"vendors": {}}, ["hubspot"], today=TODAY
    )
    hits = [c for c in cands if c.signal_type == "competitor_detected"]
    assert len(hits) == 1
    cand = hits[0]
    assert cand.natural_key == "competitor_detected:hubspot:2026-09"
    assert cand.title == "hubspot"
    assert cand.evidence_data == {"competitor": "hubspot"}


def test_tech_to_candidates_empty_competitors_emits_none():
    cands = tech_to_candidates(
        "acme.com", ["hubspot"], [], [], {"vendors": {}}, [], today=TODAY
    )
    assert not any(c.signal_type == "competitor_detected" for c in cands)


def test_parse_passes_fingerprint_competitors(monkeypatch):
    """parse must thread rules['competitors'] into tech_to_candidates.

    load_fingerprint_rules is imported function-locally inside parse, so the
    monkeypatch targets the fingerprint module attribute.
    """
    import src.sources.techstack.fingerprint as fingerprint

    rules = {
        "version": 1,
        "vendors": {
            "hubspot": {
                "display": "HubSpot",
                "category": ["crm"],
                "tier": "mid",
                "match": {"script_src": ["js.hs-scripts.com"]},
            }
        },
        "competitors": ["hubspot"],
    }
    monkeypatch.setattr(fingerprint, "load_fingerprint_rules", lambda: rules)
    doc = Document(
        doc_id="d1",
        source="techstack",
        url="https://acme.com/",
        domain="acme.com",
        body=(
            b"<html><head><script src='https://js.hs-scripts.com/123.js' defer>"
            b"</script></head><body><p>hello</p></body></html>"
        ),
        status=200,
    )
    cands = TechstackSource().parse(doc, Account(domain="acme.com", name="Acme"), {"today": "2026-09-04"})
    hits = [c for c in cands if c.signal_type == "competitor_detected"]
    assert len(hits) == 1
    assert hits[0].natural_key == "competitor_detected:hubspot:2026-09"
