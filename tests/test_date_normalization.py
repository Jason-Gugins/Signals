"""Task 3 (P2 roadmap): date normalization audit.

Contract under test:
- ``to_iso_date`` returns ``None`` for anything unparseable — never a floor
  or sentinel date. ``None`` means *unknown*, not "old" and not "today".
- Parser call sites must handle ``None`` explicitly: keep the candidate with
  ``evidence_data['date_raw']`` set to the raw string, or drop the date
  field. Garbage input must never fabricate a date that flips a signal's
  decay class or tier.
"""

from __future__ import annotations

from datetime import date

import pytest

from src.core.textutil import to_iso_date
from src.signals.score import decay_factor
from src.signals.taxonomy import Taxonomy

TODAY = date(2026, 8, 31)

# (input, expected) — None means unparseable/unknown, never a fabricated date.
TABLE = [
    ("2026-08-31", "2026-08-31"),
    ("08/31/2026", None),  # US slash format not supported -> unknown
    ("31/08/2026", None),  # UK slash format not supported -> unknown
    ("Aug 31, 2026", "2026-08-31"),
    ("Mon, 31 Aug 2026 10:00:00 GMT", "2026-08-31"),  # RFC2822
    ("31 Aug 2026", "2026-08-31"),
    ("1756600000", "2025-08-31"),  # epoch seconds (str)
    (1756600000, "2025-08-31"),  # epoch seconds (int)
    ("1756600000000", None),  # epoch millis unsupported -> unknown
    ("tomorrow", None),
    ("", None),
    ("   ", None),
    (None, None),
    ("Feb 30, 2026", None),  # impossible date -> None, NOT fabricated
    ("not a date", None),
    ("lorem ipsum dolor", None),
]


@pytest.mark.parametrize("raw,expected", TABLE, ids=lambda v: repr(v))
def test_to_iso_date_table(raw, expected):
    assert to_iso_date(raw) == expected


def test_relative_forms_require_today():
    assert to_iso_date("3 days ago", today=TODAY) == "2026-08-28"
    assert to_iso_date("3 days ago") is None
    assert to_iso_date("yesterday", today=TODAY) == "2026-08-30"
    assert to_iso_date("last week", today=TODAY) == "2026-08-24"


def test_successful_parses_unchanged():
    # Byte-identical outputs for already-working shapes (fixture net).
    assert to_iso_date(date(2026, 1, 5)) == "2026-01-05"
    assert to_iso_date("2026/01/05") == "2026-01-05"
    assert to_iso_date("20260105") == "2026-01-05"
    assert to_iso_date("Jan 5, 2026") == "2026-01-05"
    assert to_iso_date("5 January 2026") == "2026-01-05"


# ---------------------------------------------------------------------------
# decay_factor: garbage observed_at must NOT decay to the floor silently
# (floor = looks decades old). None-aware decay means "no decay penalty
# applied" for unknown dates until the date is known.
# ---------------------------------------------------------------------------

TAX = Taxonomy.load("config/signals.yaml")


def test_decay_factor_garbage_is_none_not_floor():
    floor = 0.02
    good = decay_factor("2026-08-01", TODAY, TAX.get("funding_round").half_life_days, floor)
    garbage = decay_factor("not a date", TODAY, TAX.get("funding_round").half_life_days, floor)
    assert good is not None and good > floor
    assert garbage is None


def test_tier_garbage_date_does_not_flip_tier():
    from src.core.models import Signal

    from src.signals.tier import assign_tier

    tax = TAX
    cfg = {
        "tiers": {
            "tier1": {"min_score": 70, "or_urgency": 8},
            "tier2": {"min_score": 45},
            "tier3": {"min_score": 20},
        },
        "buying_window": {"active_days": 30, "opening_days": 90, "developing_days": 180},
    }

    def sig(observed_at, signal_type=None):
        spec = tax.get(signal_type or "funding_round")
        return Signal(
            signal_id="s1",
            domain="acme.com",
            signal_type=spec.key,
            category=spec.category,
            origin=spec.origin,
            catalyst=spec.catalyst,
            polarity=spec.polarity,
            observed_at=observed_at,
            source="news",
            degree=spec.degree,
            title="t",
            confidence=0.9,
        )

    from src.signals.score import ScoreResult

    fresh = assign_tier([sig("2026-08-20")], ScoreResult(0.0, 0.0, [], [], 0, 1.0), taxonomy=tax, cfg=cfg, today=TODAY)
    garbage = assign_tier([sig("not a date")], ScoreResult(0.0, 0.0, [], [], 0, 1.0), taxonomy=tax, cfg=cfg, today=TODAY)
    # Unknown date is window-NEUTRAL: it must NOT behave like the freshest
    # signal (age 0). A garbage-only account gets no 'active' window and no
    # tier above what the known evidence supports (not tier 1).
    assert garbage.buying_window != "active"
    assert garbage.tier != 1
    # The known-age case keeps its real (better) standing: garbage is treated
    # as not-newer-than-known, not as an automatic promotion.
    assert fresh.tier <= garbage.tier or fresh.buying_window == garbage.buying_window

    # False-positive path: garbage primary + fresh external (<=90d) must NOT
    # reach tier 1 — the unknown-age primary cannot anchor prim_int_30/90.
    # (The fresh external may still drive the window: it is real, known-age
    # evidence. The bug was tier-1 eligibility via unknown-age primary.)
    combo = assign_tier(
        [sig("not a date"), sig("2026-08-20", "competitor_outage")],
        ScoreResult(0.0, 0.0, [], [], 0, 1.0),
        taxonomy=tax, cfg=cfg, today=TODAY,
    )
    assert combo.tier != 1


# ---------------------------------------------------------------------------
# Per-parser: garbage date keeps the candidate with evidence_data['date_raw']
# rather than fabricating a date.
# ---------------------------------------------------------------------------


def test_news_classify_garbage_date_keeps_candidate_with_date_raw():
    from src.core.models import Account

    from src.sources.news.classify import classify_news
    from src.sources.news.feeds import NewsItem

    acct = Account(domain="acme.com", name="Acme")
    item = NewsItem(
        title="Acme raises $4.5 million Series B",
        link="https://ex/raw",
        published="definitely not a date",
        summary="",
        source_name="TechDaily",
    )
    cand = classify_news(item, acct, today=TODAY)
    assert cand is not None
    assert cand.evidence_data.get("date_raw") == "definitely not a date"
    # observed_at stays unknown (empty) rather than a fabricated floor/today
    # date when the publisher date is garbage.
    assert not cand.observed_at


def test_news_classify_feb30_keeps_date_raw_not_fabricated():
    from src.core.models import Account

    from src.sources.news.classify import classify_news
    from src.sources.news.feeds import NewsItem

    acct = Account(domain="acme.com", name="Acme")
    item = NewsItem(
        title="Acme launches Widget 2",
        link="https://ex/feb30",
        published="Feb 30, 2026",
        summary="",
        source_name="TechDaily",
    )
    cand = classify_news(item, acct, today=TODAY)
    assert cand is not None
    assert cand.evidence_data.get("date_raw") == "Feb 30, 2026"
    assert not cand.observed_at


def test_news_classify_valid_date_no_date_raw():
    from src.core.models import Account

    from src.sources.news.classify import classify_news
    from src.sources.news.feeds import NewsItem

    acct = Account(domain="acme.com", name="Acme")
    item = NewsItem(
        title="Acme acquires Widget Labs",
        link="https://ex/ok",
        published="2026-08-20",
        summary="",
        source_name="X",
    )
    cand = classify_news(item, acct, today=TODAY)
    assert cand is not None
    assert cand.observed_at == "2026-08-20"
    assert "date_raw" not in cand.evidence_data


def test_news_classify_missing_date_still_falls_back_to_today():
    from src.core.models import Account

    from src.sources.news.classify import classify_news
    from src.sources.news.feeds import NewsItem

    acct = Account(domain="acme.com", name="Acme")
    item = NewsItem(
        title="Acme appoints Jane Doe as CEO",
        link="https://ex/nodate",
        published=None,
        summary="",
        source_name="X",
    )
    cand = classify_news(item, acct, today=TODAY)
    assert cand is not None
    assert cand.observed_at == TODAY.isoformat()
    assert "date_raw" not in cand.evidence_data


def test_linkedin_jobs_garbage_date_keeps_candidate_with_date_raw():
    from src.sources.linkedin_db.jobs import linkedin_jobs_to_candidates

    rows = [
        {
            "job_id": "j1",
            "title": "VP Engineering",
            "listed_at": "posted recently-ish",
            "url": "https://ex/j1",
        }
    ]
    out = linkedin_jobs_to_candidates(rows, "acme.com", today=TODAY)
    assert len(out) == 1
    assert out[0].evidence_data.get("date_raw") == "posted recently-ish"
    assert not out[0].observed_at


def test_linkedin_jobs_valid_date_no_date_raw():
    from src.sources.linkedin_db.jobs import linkedin_jobs_to_candidates

    rows = [
        {
            "job_id": "j2",
            "title": "VP Engineering",
            "listed_at": "2026-08-15",
            "url": "https://ex/j2",
        }
    ]
    out = linkedin_jobs_to_candidates(rows, "acme.com", today=TODAY)
    assert len(out) == 1
    assert out[0].observed_at == "2026-08-15"
    assert "date_raw" not in out[0].evidence_data


def test_linkedin_jobs_old_listing_still_skipped():
    from src.sources.linkedin_db.jobs import linkedin_jobs_to_candidates

    rows = [
        {
            "job_id": "j3",
            "title": "VP Engineering",
            "listed_at": "2026-01-01",
            "url": "https://ex/j3",
        }
    ]
    out = linkedin_jobs_to_candidates(rows, "acme.com", today=TODAY)
    assert out == []


def test_linkedin_people_garbage_role_end_keeps_with_date_raw():
    from datetime import date

    from src.sources.linkedin_db.people import to_iso_or_raw

    iso, raw = to_iso_or_raw("sometime last quarter", date(2026, 8, 31))
    assert iso is None
    assert raw == "sometime last quarter"

    iso, raw = to_iso_or_raw("2026-07-01", date(2026, 8, 31))
    assert iso == "2026-07-01"
    assert raw is None
