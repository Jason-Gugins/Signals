"""Per-tier alert routing rules (Plan Task 12)."""

import json

import pytest

from src.core.config import Config
from src.export.alerts import Alert, deliver_alerts
import src.export.alerts as alerts_mod


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch):
    """Keep repo .env vars out of os.environ (Config.load calls load_dotenv)."""
    monkeypatch.setattr("src.core.config.load_dotenv", lambda *a, **k: False)


@pytest.fixture(autouse=True)
def _clear_dedupe():
    alerts_mod._DELIVERED.clear()
    yield
    alerts_mod._DELIVERED.clear()


def _alert(tier: int, domain: str = "acme.com") -> Alert:
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
    """Fake HTTP client recording every post."""

    def __init__(self, status_code: int = 200):
        self.status_code = status_code
        self.posts: list[tuple[str, dict]] = []

    def post(self, url, json=None, timeout=None, headers=None, content=None):
        self.posts.append((url, json))
        from types import SimpleNamespace

        return SimpleNamespace(status_code=self.status_code)

    def close(self):
        pass


WEBHOOKS = [
    {"url": "http://slack.example/hook", "format": "slack"},
    {"url": "http://json.example/hook", "format": "json"},
]

ROUTES = [
    {"min_tier": 1, "max_tier": 1, "webhooks": WEBHOOKS},          # immediate
    {"min_tier": 3, "max_tier": 4, "webhooks": [], "digest": True},  # digest-only
]


def test_routes_none_is_legacy_behavior():
    """routes absent → every alert goes to every webhook (unchanged)."""
    client = RecordingClient()
    alerts = [_alert(1), _alert(3)]
    sent = deliver_alerts(alerts, WEBHOOKS, client=client)
    assert sent == 4  # 2 alerts x 2 webhooks
    assert len(client.posts) == 4


def test_routes_empty_list_is_legacy_behavior():
    client = RecordingClient()
    sent = deliver_alerts([_alert(1)], WEBHOOKS, routes=[], client=client)
    assert sent == 2


def test_tier1_routes_to_immediate_webhook_set():
    client = RecordingClient()
    sent = deliver_alerts([_alert(1)], WEBHOOKS, routes=ROUTES, client=client)
    assert sent == 2
    urls = {u for u, _ in client.posts}
    assert urls == {"http://slack.example/hook", "http://json.example/hook"}


def test_tier3_goes_to_digest_only_route_no_webhook_post():
    client = RecordingClient()
    sent = deliver_alerts([_alert(3)], WEBHOOKS, routes=ROUTES, client=client)
    assert sent == 0
    assert client.posts == []


def test_out_of_range_tier_delivers_nothing():
    client = RecordingClient()
    routes = [{"min_tier": 1, "max_tier": 2, "webhooks": WEBHOOKS}]
    sent = deliver_alerts([_alert(3)], WEBHOOKS, routes=routes, client=client)
    assert sent == 0
    assert client.posts == []


def test_routes_partition_by_tier():
    client = RecordingClient()
    alerts = [_alert(1), _alert(3)]
    deliver_alerts(alerts, WEBHOOKS, routes=ROUTES, client=client)
    # only the tier-1 alert was posted (2 webhook posts), tier-3 handled by digest
    assert len(client.posts) == 2


def test_config_parses_alert_routes_json(monkeypatch):
    monkeypatch.setenv(
        "ALERT_ROUTES_JSON",
        json.dumps([{"min_tier": 1, "max_tier": 1, "webhooks": WEBHOOKS}]),
    )
    cfg = Config.load(yaml_path=None)
    assert cfg.alert_routes == [{"min_tier": 1, "max_tier": 1, "webhooks": WEBHOOKS}]


def test_config_alert_routes_default_empty():
    cfg = Config.load(yaml_path=None)
    assert cfg.alert_routes == []


def test_config_invalid_routes_json_warns_and_empties(monkeypatch, caplog):
    monkeypatch.setenv("ALERT_ROUTES_JSON", "not json at all{")
    import logging

    with caplog.at_level(logging.WARNING, logger="src.core.config"):
        cfg = Config.load(yaml_path=None)
    assert cfg.alert_routes == []
    assert any("ALERT_ROUTES_JSON" in r.message for r in caplog.records)
