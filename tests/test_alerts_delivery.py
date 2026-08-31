"""Delivery-hardening tests for src/export/alerts.post_webhook (mocked HTTP)."""

import urllib.error
from urllib.request import Request

import pytest

from src.export.alerts import Alert, post_webhook


def _alert(domain="acme.com", typ="funding_round", at="2026-08-30"):
    return Alert(
        domain=domain, company=domain, tier=1, score=80.0, signal_type=typ,
        evidence="raised Series B", url=None, play="growth_pitch",
        urgency=1, at=at,
    )


class FakeResp:
    def __init__(self, status):
        self.status_code = status


class FakeClient:
    """Minimal stand-in for httpx.Client; records posts and scripted statuses."""

    def __init__(self, statuses, exc=None):
        # statuses: list of status codes (or None to raise exc) consumed per call
        self.statuses = list(statuses)
        self.exc = exc
        self.calls = []
        self.closed = False

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json, timeout))
        status = self.statuses.pop(0) if self.statuses else 500
        if status is None:
            raise self.exc or urllib.error.URLError("boom")
        return FakeResp(status)

    def close(self):
        self.closed = True


class NoSleep:
    """Injectable sleep: records requested delays instead of sleeping."""

    def __init__(self):
        self.delays = []

    def __call__(self, seconds):
        self.delays.append(seconds)


@pytest.fixture(autouse=True)
def _reset_dedupe():
    from src.export import alerts as alerts_mod

    alerts_mod._DELIVERED.clear()
    yield
    alerts_mod._DELIVERED.clear()


def test_success_first_try_single_call():
    c = FakeClient([200])
    sent = post_webhook([_alert()], "https://hooks.example/x", client=c, sleep=NoSleep())
    assert sent == 1
    assert len(c.calls) == 1


def test_retry_500_then_success_three_attempts():
    c = FakeClient([500, 500, 200])
    sleep = NoSleep()
    sent = post_webhook([_alert()], "https://hooks.example/x", client=c, sleep=sleep)
    assert sent == 1
    assert len(c.calls) == 3
    # exponential backoff with jitter: base 1s, 2s (±20%)
    assert len(sleep.delays) == 2
    assert 0.8 <= sleep.delays[0] <= 1.2
    assert 1.6 <= sleep.delays[1] <= 2.4


def test_all_fail_no_exception():
    c = FakeClient([500, 500, 500])
    sent = post_webhook([_alert()], "https://hooks.example/x", client=c, sleep=NoSleep())
    assert sent == 0
    assert len(c.calls) == 3


def test_connection_error_retried_not_raised():
    c = FakeClient([None, None, None], exc=urllib.error.URLError("conn refused"))
    sent = post_webhook([_alert()], "https://hooks.example/x", client=c, sleep=NoSleep())
    assert sent == 0
    assert len(c.calls) == 3


def test_invalid_url_skipped(caplog):
    c = FakeClient([])
    sent = post_webhook([_alert()], "not-a-url", client=c, sleep=NoSleep())
    assert sent == 0
    assert len(c.calls) == 0
    sent = post_webhook([_alert()], "ftp://hooks.example/x", client=c, sleep=NoSleep())
    assert sent == 0
    assert len(c.calls) == 0


def test_dedupe_second_same_key_skipped():
    c = FakeClient([200, 200])
    sent1 = post_webhook([_alert()], "https://hooks.example/x", client=c, sleep=NoSleep())
    sent2 = post_webhook([_alert()], "https://hooks.example/x", client=c, sleep=NoSleep())
    assert sent1 == 1
    assert sent2 == 0
    assert len(c.calls) == 1  # only one HTTP call total


def test_dedupe_different_keys_both_sent():
    c = FakeClient([200, 200])
    sent = post_webhook([_alert(typ="hiring_surge"), _alert(typ="funding_round")],
                        "https://hooks.example/x", client=c, sleep=NoSleep())
    assert sent == 2


def test_timeout_passed_to_call():
    c = FakeClient([200])
    sent = post_webhook([_alert()], "https://hooks.example/x", client=c, timeout=3, sleep=NoSleep())
    assert sent == 1
    assert c.calls[0][2] == 3


def test_dedupe_window_expiry(monkeypatch):
    # monkeypatch time.time so the first delivery is old relative to the second post
    from src.export import alerts as alerts_mod

    t = [1_000_000.0]

    def fake_time():
        return t[0]

    monkeypatch.setattr(alerts_mod.time, "time", fake_time)
    c = FakeClient([200, 200])
    sleep = NoSleep()
    sent1 = post_webhook([_alert()], "https://hooks.example/x", client=c, sleep=sleep, dedupe_window_h=24)
    t[0] += 23 * 3600
    sent2 = post_webhook([_alert()], "https://hooks.example/x", client=c, sleep=sleep, dedupe_window_h=24)
    assert sent1 == 1 and sent2 == 0  # still inside window
    t[0] += 2 * 3600  # now >24h after delivery
    sent3 = post_webhook([_alert()], "https://hooks.example/x", client=c, sleep=sleep, dedupe_window_h=24)
    assert sent3 == 1
    assert len(c.calls) == 2


def test_default_httpx_client_path(tmp_path, monkeypatch):
    # without a client argument, post_webhook builds its own httpx.Client;
    # patch httpx.Client to a fake to verify it is constructed with the timeout.
    import httpx

    created = {}

    class FakeRealClient:
        def __init__(self, timeout=None):
            created["timeout"] = timeout
            self.statuses = [200]

        def post(self, url, json=None, timeout=None):
            created["url"] = url
            return FakeResp(self.statuses.pop(0))

        def close(self):
            created["closed"] = True

    monkeypatch.setattr(httpx, "Client", FakeRealClient)
    sent = post_webhook([_alert()], "https://hooks.example/x", sleep=NoSleep(), timeout=7)
    assert sent == 1
    assert created == {"timeout": 7, "url": "https://hooks.example/x", "closed": True}
