"""Tests for the self-improving routing state machine (Task 5).

Pure, no network, no real-clock sleeps — time travel via the injected clock
or force_state().
"""

import json

import pytest

from src.antibot.python.routing import RouteState


@pytest.fixture
def fake_clock():
    """Controllable clock: returns a mutable holder; advance it as needed."""
    holder = {"now": 1_000_000.0}
    return holder


def make_state(tmp_path, clock, **kw):
    return RouteState(str(tmp_path / "routing.json"), clock=lambda: clock["now"], **kw)


def test_first_visit_routes_cold(tmp_path):
    r = RouteState(str(tmp_path / "routing.json"))
    assert r.decide("g2.com") == "Cold"


def test_warm_cookies_skip_browser(tmp_path, fake_clock):
    r = make_state(tmp_path, fake_clock)
    r.record_solve("g2.com", cookies=["datadome"])
    assert r.decide("g2.com") == "Warm"


def test_expired_cookies_skip_doomed_tier1(tmp_path, fake_clock):
    r = make_state(tmp_path, fake_clock)
    r.record_solve("g2.com", cookies=["datadome"])
    r.expire("g2.com")
    assert r.decide("g2.com") == "SkipToSolve"


def test_lifetime_learning_converges(tmp_path, fake_clock):
    r = make_state(tmp_path, fake_clock)
    r.record_solve("g2.com", cookies=["datadome"])
    r.expire("g2.com", after_seconds=3600)
    r.record_solve("g2.com", cookies=["datadome"])
    r.expire("g2.com", after_seconds=1800)
    assert r.observed_lifetime("g2.com") == 1800


def test_lifetime_learning_never_increases(tmp_path, fake_clock):
    r = make_state(tmp_path, fake_clock)
    r.record_solve("g2.com", cookies=["datadome"])
    r.expire("g2.com", after_seconds=3600)
    r.record_solve("g2.com", cookies=["datadome"])
    r.expire("g2.com", after_seconds=7200)
    assert r.observed_lifetime("g2.com") == 3600


def test_periodic_cold_recheck(tmp_path, fake_clock):
    r = make_state(tmp_path, fake_clock)
    r.record_solve("g2.com", cookies=["datadome"])
    r.force_state("g2.com", "SkipToSolve", age_seconds=26 * 3600)
    assert r.decide("g2.com") == "RecheckCold"


def test_recheck_cold_within_default_window(tmp_path, fake_clock):
    r = make_state(tmp_path, fake_clock)
    r.record_solve("g2.com", cookies=["datadome"])
    r.expire("g2.com")
    r.force_state("g2.com", "SkipToSolve", age_seconds=3600)
    assert r.decide("g2.com") == "SkipToSolve"


def test_persistence_roundtrip(tmp_path, fake_clock):
    path = str(tmp_path / "routing.json")
    r1 = RouteState(path, clock=lambda: fake_clock["now"])
    r1.record_solve("g2.com", cookies=["datadome"])
    r1.expire("g2.com", after_seconds=1800)
    r2 = RouteState(path, clock=lambda: fake_clock["now"])
    assert r2.decide("g2.com") == "SkipToSolve"
    assert r2.observed_lifetime("g2.com") == 1800
    data = json.loads((tmp_path / "routing.json").read_text())
    assert "g2.com" in data


def test_corrupt_file_fresh_state(tmp_path):
    path = tmp_path / "routing.json"
    path.write_text("{not valid json!!!")
    r = RouteState(str(path))
    assert r.decide("g2.com") == "Cold"
    # and a mutation overwrites the corrupt file cleanly
    r.record_solve("g2.com", cookies=["datadome"])
    assert r.decide("g2.com") == "Warm"