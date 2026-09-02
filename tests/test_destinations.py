"""Export destination plugins (Plan Task 14)."""

import json
import os

import pytest

from src.core.config import Config
from src.export.alerts import Alert
import src.export.alerts as alerts_mod
from src.export.destinations import (
    FileDestination,
    WebhookDestination,
    load_destinations,
)


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch):
    """Keep repo .env vars out of os.environ (Config.load calls load_dotenv)."""
    monkeypatch.setattr("src.core.config.load_dotenv", lambda *a, **k: False)


@pytest.fixture(autouse=True)
def _clear_dedupe():
    alerts_mod._DELIVERED.clear()
    yield
    alerts_mod._DELIVERED.clear()


def _alert(tier: int = 1, domain: str = "acme.com") -> Alert:
    return Alert(
        domain=domain,
        company=domain.split(".")[0].title(),
        tier=tier,
        score=80.0,
        signal_type="funding_round",
        evidence="raised Series B",
        url=None,
        play=None,
        urgency=1,
        at="2026-09-01T00:00:00Z",
    )


class RecordingClient:
    def __init__(self):
        self.posts: list[tuple[str, dict, dict | None, bytes | None]] = []

    def post(self, url, json=None, timeout=None, headers=None, content=None):
        self.posts.append((url, json, headers, content))
        from types import SimpleNamespace

        return SimpleNamespace(status_code=200)

    def close(self):
        pass


def _config(tmp_path, destinations=None) -> Config:
    cfg = Config.load(yaml_path=None)
    cfg.storage.alerts_dir = str(tmp_path / "alerts")
    if destinations is not None:
        cfg.exports.destinations = destinations
    return cfg


def test_default_config_destination_is_file_only():
    cfg = Config.load(yaml_path=None)
    dests = load_destinations(cfg)
    assert len(dests) == 1
    assert isinstance(dests[0], FileDestination)


def test_file_destination_writes_jsonl(tmp_path):
    cfg = _config(tmp_path)
    dest = FileDestination()
    items = [_alert(), _alert(domain="globex.com")]
    out = dest.deliver(items, cfg)
    assert os.path.isfile(out)
    lines = (tmp_path / "alerts" / "alerts.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["domain"] == "acme.com"


def test_file_destination_writes_digest_text(tmp_path):
    cfg = _config(tmp_path)
    dest = FileDestination(filename="digest.txt")
    out = dest.deliver("DIGEST BODY", cfg)
    # Append semantics (P3 review fix): a trailing newline is normalized in.
    assert (tmp_path / "alerts" / "digest.txt").read_text(encoding="utf-8") == "DIGEST BODY\n"
    assert out.endswith("digest.txt")


def test_webhook_destination_reuses_signed_path(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_SIGNING_SECRET", "sekrit")
    cfg = _config(tmp_path)
    client = RecordingClient()
    dest = WebhookDestination(
        webhooks=[{"url": "http://json.example/hook", "format": "json",
                   "secret_env": "TEST_SIGNING_SECRET"}],
        client=client,
    )
    result = dest.deliver([_alert()], cfg)
    assert "webhook" in result
    assert len(client.posts) == 1
    url, payload, headers, body = client.posts[0]
    assert url == "http://json.example/hook"
    assert headers is not None and "X-Signature" in headers
    assert body is not None
    import hashlib
    import hmac as hmac_mod

    expected = hmac_mod.new(b"sekrit", body, hashlib.sha256).hexdigest()
    assert headers["X-Signature"] == "sha256=" + expected


def test_webhook_destination_uses_config_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_SIGNING_SECRET", "sekrit")
    cfg = _config(tmp_path)
    client = RecordingClient()
    dest = WebhookDestination(
        webhooks=[{"url": "http://json.example/hook", "format": "json"}],
        routes=[{"min_tier": 1, "max_tier": 1, "webhooks": [{"url": "http://json.example/hook", "format": "json"}]}],
        client=client,
    )
    dest.deliver([_alert(), _alert(tier=3)], cfg)
    assert len(client.posts) == 1  # only tier-1 alert posted


def test_load_destinations_unknown_type_skipped(tmp_path):
    cfg = _config(tmp_path, destinations=[{"type": "carrier_pigeon"}, {"type": "file"}])
    dests = load_destinations(cfg)
    assert len(dests) == 1
    assert isinstance(dests[0], FileDestination)


def test_load_destinations_webhook_type(tmp_path):
    cfg = _config(tmp_path, destinations=[{"type": "webhook"}])
    dests = load_destinations(cfg)
    assert len(dests) == 1
    assert isinstance(dests[0], WebhookDestination)
