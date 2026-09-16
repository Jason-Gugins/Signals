"""``utc_today()``: the age reference follows UTC, not the local clock (Finding 7)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from src.core.timeutil import utc_today


def test_utc_today_follows_utc_not_local():
    """An instant whose UTC date differs from the local date returns the UTC date.

    2026-09-15T01:00Z is still 2026-09-14 on this machine (UTC-4); the age
    reference must be the UTC date because ``observed_at`` values are
    aware-UTC ISO strings.  Pre-fix there was no such helper: the reference came
    from ``date.today()``, i.e. 2026-09-14 for this instant.
    """
    instant = datetime(2026, 9, 15, 1, 0, tzinfo=timezone.utc)
    assert utc_today(now=instant) == date(2026, 9, 15)
    # The two really do disagree at this instant — that is the bug being pinned.
    local = instant.astimezone(timezone(timedelta(hours=-4)))
    assert local.date() == date(2026, 9, 14)


def test_utc_today_defaults_to_now_utc():
    """With no argument the reference is the current UTC date."""
    before = datetime.now(timezone.utc).date()
    got = utc_today()
    after = datetime.now(timezone.utc).date()
    assert isinstance(got, date)
    # Accept either side of a UTC-midnight rollover — never flake.
    assert got in {before, after}
