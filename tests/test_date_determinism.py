def test_today_defaults_to_utc_clock():
    """The fallback is the UTC date — the age reference, not ``date.today()``.

    Pre-fix this test asserted ``_today() == date.today()`` (the local clock),
    which is exactly the Finding-7 bug: on a UTC-4 machine after local midnight
    the local date lags the UTC date that ``observed_at`` values are written in.
    Accept either side of a UTC-midnight rollover so it can never flake.
    """
    from datetime import datetime, timezone

    from src.pipeline import orchestrator

    # reset any injection from other tests
    orchestrator.set_today(None)
    before = datetime.now(timezone.utc).date()
    got = orchestrator._today()
    after = datetime.now(timezone.utc).date()
    assert got in {before, after}


def test_today_injectable():
    from datetime import date
    from src.pipeline import orchestrator

    orchestrator.set_today(date(2026, 9, 1))
    try:
        assert orchestrator._today() == date(2026, 9, 1)
    finally:
        orchestrator.set_today(None)
