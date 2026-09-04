"""Persistence + eviction tests for the alert-delivery dedupe store (Plan Task 10).

The delivered-key dedupe in src/export/alerts must survive process restarts
(persisted JSON store, atomic write) and evict entries older than the dedupe
window on every write. All tests use tmp_path stores — never the real data/
directory.
"""

import json
import time

import pytest

from src.export import alerts as alerts_mod
from src.export.alerts import Alert, post_webhook

URL = "https://hooks.example/x"


def _alert(domain="acme.com", typ="funding_round", at="2026-08-30"):
    return Alert(
        domain=domain, company=domain, tier=1, score=80.0, signal_type=typ,
        evidence="raised Series B", url=None, play="growth_pitch",
        urgency=1, at=at,
    )


def _key(domain, typ="funding_round", at="2026-08-30", url=URL):
    return alerts_mod._dedupe_key_to_str((url, domain, typ, at))


class FakeResp:
    def __init__(self, status):
        self.status_code = status


class FakeClient:
    """Minimal httpx.Client stand-in: 200 on every post, records calls."""

    def __init__(self):
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json, timeout))
        return FakeResp(200)

    def close(self):
        pass


class NoSleep:
    def __init__(self):
        self.delays = []

    def __call__(self, seconds):
        self.delays.append(seconds)


@pytest.fixture(autouse=True)
def _isolated_dedupe():
    """Fresh in-memory state per test; restore the module's load cache after."""
    alerts_mod._DELIVERED.clear()
    saved_loaded_path = alerts_mod._DEDUPE_LOADED_PATH
    alerts_mod._DEDUPE_LOADED_PATH = None
    yield
    alerts_mod._DELIVERED.clear()
    alerts_mod._DEDUPE_LOADED_PATH = saved_loaded_path


def test_mark_delivered_persists_key_and_timestamp(tmp_path):
    store = tmp_path / "alerts_dedupe.json"
    sent = post_webhook([_alert()], URL, client=FakeClient(), sleep=NoSleep(), dedupe_path=store)
    assert sent == 1
    assert store.exists()
    data = json.loads(store.read_text(encoding="utf-8"))
    assert _key("acme.com") in data
    ts = data[_key("acme.com")]
    assert isinstance(ts, float)
    assert 0 < ts <= time.time()


def test_restart_reloads_store_and_skips_redelivery(tmp_path):
    store = tmp_path / "alerts_dedupe.json"
    post_webhook([_alert()], URL, client=FakeClient(), sleep=NoSleep(), dedupe_path=store)

    # Simulate a process restart: empty memory, drop the per-path load cache.
    alerts_mod._DELIVERED.clear()
    alerts_mod._DEDUPE_LOADED_PATH = None

    c = FakeClient()
    sent = post_webhook([_alert()], URL, client=c, sleep=NoSleep(), dedupe_path=store)
    assert sent == 0
    assert c.calls == []  # nothing re-posted within the 24h window


def test_stale_entries_pruned_on_write(tmp_path):
    store = tmp_path / "alerts_dedupe.json"
    stale_ts = time.time() - 25 * 3600  # older than the 24h dedupe window
    store.write_text(
        json.dumps({_key("old.com"): stale_ts, _key("keep.com"): time.time()}),
        encoding="utf-8",
    )

    # A fresh mark triggers a save, which prunes stale entries from the file.
    sent = post_webhook(
        [_alert(domain="fresh.com")], URL, client=FakeClient(), sleep=NoSleep(), dedupe_path=store
    )
    assert sent == 1
    data = json.loads(store.read_text(encoding="utf-8"))
    assert _key("old.com") not in data
    assert _key("keep.com") in data
    assert _key("fresh.com") in data


def test_corrupt_store_fails_open_and_delivery_works(tmp_path):
    store = tmp_path / "alerts_dedupe.json"
    store.write_text("{not valid json!!", encoding="utf-8")

    c = FakeClient()
    sent = post_webhook([_alert()], URL, client=c, sleep=NoSleep(), dedupe_path=store)
    assert sent == 1
    assert len(c.calls) == 1
    # The next mark rewrote a clean, readable store.
    data = json.loads(store.read_text(encoding="utf-8"))
    assert _key("acme.com") in data


def test_unwritable_store_does_not_break_delivery(tmp_path):
    store = tmp_path / "store_is_a_dir"
    store.mkdir()  # os.replace onto a directory fails -> persistence error

    c = FakeClient()
    sent = post_webhook([_alert()], URL, client=c, sleep=NoSleep(), dedupe_path=store)
    assert sent == 1
    assert len(c.calls) == 1
    # In-memory behavior is identical even though persistence failed.
    assert ("https://hooks.example/x", "acme.com", "funding_round", "2026-08-30") in alerts_mod._DELIVERED
