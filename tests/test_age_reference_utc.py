"""Regression tests for Finding 7 — the age reference must be UTC, derived once.

Observed failure (2026-09-15 smoke run): a signal observed at
``2026-09-15T00:30:00+00:00`` was handed the LOCAL reference date 2026-09-14
(this machine is UTC-4), so every consumer saw age ``-1`` for a brand-new
signal and ``humanize_age`` rendered ``"on 2026-09-15"`` instead of
``"today"``.

The fix normalises the reference ONCE at its source (``src/core/timeutil.py``)
and leaves the five consumers' arithmetic untouched — they already guard a
genuinely future-dated observation.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from src.core.models import Signal
from src.pipeline import orchestrator
from src.signals.combos import condition_matches
from src.signals.evidence import humanize_age, render_evidence
from src.signals.lifecycle import is_superseded
from src.signals.score import decay_factor
from src.signals.tier import _age

OBSERVED = "2026-09-15T00:30:00+00:00"
UTC_REFERENCE = date(2026, 9, 15)
LOCAL_REFERENCE = date(2026, 9, 14)  # same instant, local (UTC-4) date
HALF_LIFE = 45
FLOOR = 0.02

SIG = Signal(
    signal_id="s-utc",
    domain="acme.com",
    signal_type="funding_round",
    category="financial",
    origin="external",
    catalyst="primary",
    polarity="positive",
    observed_at=OBSERVED,
    source="t",
    title="Raised a round",
    confidence=0.9,
)


class _LocalClock(date):
    """A ``date`` whose ``.today()`` is the local date at the failure instant."""

    @classmethod
    def today(cls) -> date:
        return LOCAL_REFERENCE


class _FrozenUtcDateTime(datetime):
    """A ``datetime`` whose ``.now()`` is frozen at 2026-09-15T00:30Z."""

    @classmethod
    def now(cls, tz=None) -> datetime:
        return datetime(2026, 9, 15, 0, 30, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# The regression: the reference handed to the five age consumers is UTC.
# --------------------------------------------------------------------------


def test_utc_ahead_observation_is_not_a_negative_age():
    """A fresh UTC-ahead observation is age 0, not -1, across all five consumers.

    Pre-fix this scenario produced (with the local reference 2026-09-14):
    ``humanize_age`` -> ``"on 2026-09-15"``; ``decay_factor`` -> 1.0 (the
    ``age <= 0`` guard, NOT the floor); ``tier._age`` -> -1; combos still kept
    the signal (explicit future guard); ``is_superseded`` -> False. The five
    assertions below therefore cannot fail before the change: the fix is at the
    REFERENCE producer, and the consumers' arithmetic is deliberately
    unchanged. They lock in that a UTC-correct reference is handled sanely.
    """
    orchestrator.set_today(UTC_REFERENCE)
    try:
        # The injected hook is authoritative and now yields the UTC date.
        today = orchestrator._today()
        assert today == UTC_REFERENCE
        assert humanize_age(OBSERVED, today) == "today"
        assert decay_factor(OBSERVED, today, HALF_LIFE, FLOOR) == 1.0
        assert _age(SIG, today) == 0
        assert condition_matches([SIG], {"within_days": 7}, today=today) == [SIG]
        assert (
            is_superseded(SIG, today=today, supersede_days_by_type={"funding_round": 30})
            is False
        )
    finally:
        orchestrator.set_today(None)


def test_local_reference_reproduces_the_observed_failure():
    """Documents the pre-fix arithmetic the fix removes at the source.

    With the LOCAL reference (2026-09-14) the fresh signal is one day "in the
    future": age -1 and ``"on 2026-09-15"``. This test stays green before AND
    after the change — it is the reproduction record, not the regression guard
    (the guard is ``test_utc_ahead_observation_is_not_a_negative_age`` above).
    """
    assert (LOCAL_REFERENCE - date(2026, 9, 15)).days == -1
    assert humanize_age(OBSERVED, LOCAL_REFERENCE) == "on 2026-09-15"
    assert _age(SIG, LOCAL_REFERENCE) == -1
    # The consumers' guards absorb the bad reference rather than exploding:
    assert decay_factor(OBSERVED, LOCAL_REFERENCE, HALF_LIFE, FLOOR) == 1.0
    assert condition_matches([SIG], {"within_days": 7}, today=LOCAL_REFERENCE) == [SIG]
    assert (
        is_superseded(SIG, today=LOCAL_REFERENCE, supersede_days_by_type={"funding_round": 30})
        is False
    )


def test_render_evidence_default_reference_is_utc(monkeypatch):
    """``render_evidence``'s DEFAULT reference comes from the UTC clock.

    The local clock is pinned to 2026-09-14 (what ``date.today()`` returned at
    the instant of the failure) and the UTC clock to 2026-09-15T00:30Z, so the
    two disagree exactly as they did in the smoke run. Pre-fix this test failed
    with ``"on 2026-09-15"``; post-fix ``evidence.py`` does not read the local
    clock at all. (Pre-fix the timeutil patch below also cannot resolve — the
    module is new.)
    """
    monkeypatch.setattr("src.core.timeutil.datetime", _FrozenUtcDateTime)
    monkeypatch.setattr("src.signals.evidence.date", _LocalClock)
    text = render_evidence(SIG)  # no today= -> producer fallback path
    assert "today" in text
    assert "on 2026-09-15" not in text


# --------------------------------------------------------------------------
# The producer: the fallback is UTC-derived; the test hook still wins.
# --------------------------------------------------------------------------


def test_today_fallback_is_utc_derived(monkeypatch):
    """With nothing injected, ``_today()`` is the UTC date, not the local one.

    UTC clock frozen at 2026-09-15T00:30Z while the local clock is pinned to
    2026-09-14: the fallback must answer 2026-09-15. Pre-fix this test fails
    (``_today()`` returned the patched local date, and the new timeutil module
    does not exist to patch).
    """
    monkeypatch.setattr("src.core.timeutil.datetime", _FrozenUtcDateTime)
    monkeypatch.setattr("src.pipeline.orchestrator.date", _LocalClock)
    orchestrator.set_today(None)
    assert orchestrator._today() == UTC_REFERENCE


def test_injected_today_wins_over_the_fallback(monkeypatch):
    """The ``_INJECTED_TODAY`` test hook stays authoritative over the clock."""
    monkeypatch.setattr("src.pipeline.orchestrator.date", _LocalClock)
    orchestrator.set_today(UTC_REFERENCE)
    try:
        assert orchestrator._today() == UTC_REFERENCE
    finally:
        orchestrator.set_today(None)
