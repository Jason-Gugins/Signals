def test_today_defaults_to_real_clock():
    from datetime import date
    from src.pipeline import orchestrator
    # reset any injection from other tests
    orchestrator.set_today(None)
    assert orchestrator._today() == date.today()


def test_today_injectable():
    from datetime import date
    from src.pipeline import orchestrator
    orchestrator.set_today(date(2026, 9, 1))
    try:
        assert orchestrator._today() == date(2026, 9, 1)
    finally:
        orchestrator.set_today(None)
