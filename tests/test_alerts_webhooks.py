"""Tests for generic signed JSON webhooks (post_json_webhook) and ALERT_WEBHOOKS_JSON config."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from urllib.error import URLError

import pytest
from loguru import logger as loguru_logger

_json_dumps = json.dumps  # module-level alias — ``json`` is shadowed in CapturingClient.post

from src.core.config import Config
from src.export.alerts import Alert, format_slack_text, post_json_webhook, post_webhook


def _alert(domain="acme.com", typ="funding_round", at="2026-08-30"):
    return Alert(
        domain=domain, company="Acme Corp", tier=1, score=87.5, signal_type=typ,
        evidence="raised Series B", url="https://example.com/news/1", play="growth_pitch",
        urgency=1, at=at,
    )


class CapturingClient:
    """Fake HTTP client: records posts (url/json/content/timeout/headers), scripted statuses.

    Mimics httpx's dual API: accepts either ``json=`` (re-serialized) or
    ``content=`` (raw bytes) so tests can assert on the EXACT wire bytes."""

    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = []
        self.closed = False

    def post(self, url, json=None, content=None, timeout=None, headers=None):
        self.calls.append({
            "url": url, "json": json, "content": content,
            "timeout": timeout, "headers": headers or {},
            "body": content if content is not None else (
                _json_dumps(json).encode("utf-8") if json is not None else None
            ),
        })
        status = self.statuses.pop(0) if self.statuses else 500
        return type("Resp", (), {"status_code": status})()

    def close(self):
        self.closed = True


class NoSleep:
    def __init__(self):
        self.delays = []

    def __call__(self, seconds):
        self.delays.append(seconds)


class LoguruCaplog:
    """Bridge loguru -> stdlib logging so pytest caplog can see records (loguru
    does not propagate to the stdlib root logger)."""

    def __init__(self, caplog, level=logging.WARNING):
        class _PropagateHandler(logging.Handler):
            def emit(self, record):
                logging.getLogger("alerts-caplog").handle(record)

        self.handler_id = loguru_logger.add(_PropagateHandler(), level=level)
        self.caplog = caplog
        self.level = level
        self._ctx = None

    def __enter__(self):
        self._ctx = self.caplog.at_level(self.level, logger="alerts-caplog")
        return self._ctx.__enter__()

    def __exit__(self, *exc):
        ret = self._ctx.__exit__(*exc)
        loguru_logger.remove(self.handler_id)
        return ret

    def messages(self):
        return [r.getMessage() for r in self.caplog.records]


@pytest.fixture(autouse=True)
def _reset_dedupe():
    from src.export import alerts as alerts_mod

    alerts_mod._DELIVERED.clear()
    yield
    alerts_mod._DELIVERED.clear()


def _expected_signature(secret: str, body: bytes) -> str:
    """HMAC over the EXACT wire body bytes (not a re-serialized payload)."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return "sha256=" + digest


# ---------------------------------------------------------------------------
# JSON payload shape
# ---------------------------------------------------------------------------


def test_json_payload_shape_keys_and_values():
    c = CapturingClient([200])
    a = _alert()
    sent = post_json_webhook([a], "https://hooks.example/json", client=c, sleep=NoSleep())
    assert sent == 1
    assert len(c.calls) == 1
    payload = c.calls[0]["json"]
    assert set(payload.keys()) == {"type", "account", "signal", "why_now"}
    assert payload["type"] == "funding_round"
    assert set(payload["account"].keys()) == {"domain", "company", "tier", "score"}
    assert payload["account"] == {"domain": "acme.com", "company": "Acme Corp", "tier": 1, "score": 87.5}
    assert set(payload["signal"].keys()) == {"evidence", "url", "at"}
    assert payload["signal"] == {"evidence": "raised Series B", "url": "https://example.com/news/1", "at": "2026-08-30"}
    assert payload["why_now"] == "raised Series B"


def test_json_payload_one_post_per_alert():
    c = CapturingClient([200, 200])
    sent = post_json_webhook(
        [_alert(typ="funding_round"), _alert(domain="other.com", typ="hiring_surge")],
        "https://hooks.example/json", client=c, sleep=NoSleep(),
    )
    assert sent == 2
    assert len(c.calls) == 2
    assert c.calls[0]["json"]["type"] == "funding_round"
    assert c.calls[1]["json"]["type"] == "hiring_surge"


# ---------------------------------------------------------------------------
# HMAC signing
# ---------------------------------------------------------------------------


def test_hmac_signature_correctness():
    secret = "whsec_abcdef123456"
    c = CapturingClient([200])
    sent = post_json_webhook(
        [_alert()], "https://hooks.example/json", signing_secret=secret, client=c, sleep=NoSleep()
    )
    assert sent == 1
    # Sign-what-you-send: the signature must verify against the EXACT body
    # bytes that went on the wire (call["body"]), not a re-serialization of
    # the payload dict — different json encoders would otherwise break it.
    wire_body = c.calls[0]["body"]
    assert wire_body is not None
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"), wire_body, hashlib.sha256
    ).hexdigest()
    assert c.calls[0]["headers"]["X-Signature"] == expected


def test_hmac_signature_lowercase_hex():
    secret = "another-secret"
    c = CapturingClient([200])
    post_json_webhook([_alert()], "https://hooks.example/json", signing_secret=secret, client=c, sleep=NoSleep())
    sig = c.calls[0]["headers"]["X-Signature"]
    assert sig.startswith("sha256=")
    hex_part = sig[len("sha256="):]
    assert hex_part == hex_part.lower()
    int(hex_part, 16)  # valid hex
    assert len(hex_part) == 64


def test_no_signature_header_without_secret():
    c = CapturingClient([200])
    post_json_webhook([_alert()], "https://hooks.example/json", client=c, sleep=NoSleep())
    assert "X-Signature" not in c.calls[0]["headers"]


def test_signature_is_deterministic_per_body():
    secret = "whsec_x"
    c = CapturingClient([200, 200])
    a1, a2 = _alert(typ="funding_round"), _alert(domain="zz.com", typ="hiring_surge")
    post_json_webhook([a1, a2], "https://hooks.example/json", signing_secret=secret, client=c, sleep=NoSleep())
    sig1 = c.calls[0]["headers"]["X-Signature"]
    sig2 = c.calls[1]["headers"]["X-Signature"]
    # Verify against the EXACT wire bytes, not a re-serialized payload.
    assert sig1 == _expected_signature(secret, c.calls[0]["body"])
    assert sig2 == _expected_signature(secret, c.calls[1]["body"])
    assert sig1 != sig2  # different bodies -> different signatures


# ---------------------------------------------------------------------------
# Per-webhook signature options: legacy | hub | stripe
# ---------------------------------------------------------------------------


def test_legacy_signature_explicit_option_unchanged():
    """signature='legacy' (the default) pins today's header exactly."""
    secret = "whsec_legacy"
    c = CapturingClient([200])
    sent = post_json_webhook(
        [_alert()], "https://hooks.example/json",
        signing_secret=secret, signature="legacy", client=c, sleep=NoSleep(),
    )
    assert sent == 1
    headers = c.calls[0]["headers"]
    assert headers["X-Signature"] == _expected_signature(secret, c.calls[0]["body"])
    assert "X-Hub-Signature-256" not in headers
    assert "Stripe-Signature" not in headers


def test_hub_signature_header_matches_wire_body():
    secret = "whsec_hub"
    c = CapturingClient([200])
    sent = post_json_webhook(
        [_alert()], "https://hooks.example/json",
        signing_secret=secret, signature="hub", client=c, sleep=NoSleep(),
    )
    assert sent == 1
    headers = c.calls[0]["headers"]
    assert "X-Signature" not in headers
    assert "Stripe-Signature" not in headers
    # Same exact-body-bytes HMAC as legacy, under the GitHub-style header name.
    assert headers["X-Hub-Signature-256"] == _expected_signature(secret, c.calls[0]["body"])


def test_stripe_signature_scheme_with_injected_clock():
    secret = "whsec_stripe"
    c = CapturingClient([200, 200])
    a1, a2 = _alert(typ="funding_round"), _alert(domain="other.com", typ="hiring_surge")
    post_json_webhook(
        [a1], "https://hooks.example/json", signing_secret=secret,
        signature="stripe", now=1_000_000_000, client=c, sleep=NoSleep(),
    )
    post_json_webhook(
        [a2], "https://hooks.example/json", signing_secret=secret,
        signature="stripe", now=1_000_000_500, client=c, sleep=NoSleep(),
    )
    assert len(c.calls) == 2
    first, second = c.calls
    assert "X-Signature" not in first["headers"]
    assert "X-Hub-Signature-256" not in first["headers"]

    def _parse(call):
        parts = dict(p.split("=", 1) for p in call["headers"]["Stripe-Signature"].split(","))
        assert set(parts) == {"t", "v1"}
        return parts

    p1, p2 = _parse(first), _parse(second)
    assert p1["t"] == "1000000000"  # t comes from the injected clock
    assert p2["t"] == "1000000500"
    assert p1["t"] != p2["t"]  # different injected now -> different timestamp
    # v1 = HMAC-SHA256 of "<t>.<body>" over the EXACT wire bytes.
    for parts, call in ((p1, first), (p2, second)):
        expected = hmac.new(
            secret.encode("utf-8"), f"{parts['t']}.".encode("utf-8") + call["body"], hashlib.sha256
        ).hexdigest()
        assert parts["v1"] == expected
    assert p1["v1"] != p2["v1"]


def test_unknown_signature_value_falls_back_to_legacy_with_warning(caplog):
    secret = "whsec_x"
    c = CapturingClient([200])
    bridge = LoguruCaplog(caplog)
    try:
        with bridge:
            sent = post_json_webhook(
                [_alert()], "https://hooks.example/json",
                signing_secret=secret, signature="carrier_pigeon",
                client=c, sleep=NoSleep(),
            )
    except Exception:
        bridge.__exit__(None, None, None)
        raise
    assert sent == 1  # delivery is never dropped by a bad option value
    headers = c.calls[0]["headers"]
    assert headers["X-Signature"] == _expected_signature(secret, c.calls[0]["body"])
    assert "X-Hub-Signature-256" not in headers
    assert "Stripe-Signature" not in headers
    assert any("carrier_pigeon" in m for m in bridge.messages())


# ---------------------------------------------------------------------------
# Retry reuse (same hardened skeleton as post_webhook)
# ---------------------------------------------------------------------------


def test_retry_three_attempts_then_exhaustion_logged(caplog):
    c = CapturingClient([500, 500, 500])
    sleep = NoSleep()
    bridge = LoguruCaplog(caplog)
    try:
        with bridge:
            sent = post_json_webhook([_alert()], "https://hooks.example/json", client=c, sleep=sleep)
    except Exception:
        bridge.__exit__(None, None, None)
        raise
    assert sent == 0
    assert len(c.calls) == 3
    assert len(sleep.delays) == 2  # exp backoff between attempts
    assert 0.8 <= sleep.delays[0] <= 1.2
    assert 1.6 <= sleep.delays[1] <= 2.4
    assert any("3 attempts" in m or "failed after" in m for m in bridge.messages())


def test_retry_500_then_success():
    c = CapturingClient([500, 200])
    sent = post_json_webhook([_alert()], "https://hooks.example/json", client=c, sleep=NoSleep())
    assert sent == 1
    assert len(c.calls) == 2


def test_json_path_invalid_url_skipped():
    c = CapturingClient([])
    sent = post_json_webhook([_alert()], "not-a-url", client=c, sleep=NoSleep())
    assert sent == 0
    assert len(c.calls) == 0


def test_json_path_dedupe_same_key_second_call_skipped():
    c = CapturingClient([200])
    sent1 = post_json_webhook([_alert()], "https://hooks.example/json", client=c, sleep=NoSleep())
    sent2 = post_json_webhook([_alert()], "https://hooks.example/json", client=c, sleep=NoSleep())
    assert sent1 == 1
    assert sent2 == 0
    assert len(c.calls) == 1


def test_json_path_connection_error_not_raised():
    class RaisingClient(CapturingClient):
        def post(self, url, json=None, timeout=None, headers=None):
            self.calls.append({"url": url, "json": json, "timeout": timeout, "headers": headers or {}})
            raise URLError("conn refused")

    c = RaisingClient([])
    sent = post_json_webhook([_alert()], "https://hooks.example/json", client=c, sleep=NoSleep())
    assert sent == 0
    assert len(c.calls) == 3


# ---------------------------------------------------------------------------
# Legacy Slack path untouched
# ---------------------------------------------------------------------------


def test_legacy_slack_payload_unchanged():
    c = CapturingClient([200])
    a = _alert()
    sent = post_webhook([a], "https://hooks.example/slack", client=c, sleep=NoSleep())
    assert sent == 1
    assert len(c.calls) == 1
    payload = c.calls[0]["json"]
    assert set(payload.keys()) == {"text"}
    assert payload["text"] == format_slack_text(a)
    assert "Acme Corp" in payload["text"]
    assert "X-Signature" not in c.calls[0]["headers"]


def test_legacy_slack_retry_still_three_attempts():
    c = CapturingClient([500, 500, 500])
    sent = post_webhook([_alert()], "https://hooks.example/slack", client=c, sleep=NoSleep())
    assert sent == 0
    assert len(c.calls) == 3


# ---------------------------------------------------------------------------
# deliver_alerts: multi-webhook dispatch + send-time secret resolution
# ---------------------------------------------------------------------------


def test_deliver_alerts_routes_by_format(monkeypatch):
    from src.export.alerts import deliver_alerts

    monkeypatch.setenv("HOOK_SECRET_TEST", "s3cret")
    slack_c = CapturingClient([200])
    json_c = CapturingClient([200])

    sent = deliver_alerts([_alert()], [{"url": "https://hooks.example/slack", "format": "slack"}],
                          client=slack_c, sleep=NoSleep())
    sent += deliver_alerts([_alert()],
                           [{"url": "https://hooks.example/json", "format": "json", "secret_env": "HOOK_SECRET_TEST"}],
                           client=json_c, sleep=NoSleep())
    assert sent == 2
    assert set(slack_c.calls[0]["json"].keys()) == {"text"}
    # Signed JSON path sends pre-serialized body bytes (sign-what-you-send);
    # the parsed payload is recorded in "body" by the fake client.
    assert set(json.loads(json_c.calls[0]["body"]).keys()) == {"type", "account", "signal", "why_now"}
    assert "X-Signature" in json_c.calls[0]["headers"]


def test_deliver_alerts_missing_secret_env_skips_with_warning(caplog):
    from src.export.alerts import deliver_alerts

    missing = "DEFINITELY_MISSING_SECRET_ENV_XYZ"
    c = CapturingClient([])
    bridge = LoguruCaplog(caplog)
    try:
        with bridge:
            sent = deliver_alerts(
                [_alert()],
                [{"url": "https://hooks.example/json", "format": "json", "secret_env": missing}],
                client=c, sleep=NoSleep(),
            )
    except Exception:
        bridge.__exit__(None, None, None)
        raise
    assert sent == 0
    assert len(c.calls) == 0
    assert any(missing in m for m in bridge.messages())


def test_signature_option_flows_through_deliver_alerts(monkeypatch):
    """The per-webhook dict carries `signature` exactly like format/secret_env."""
    from src.export.alerts import deliver_alerts

    monkeypatch.setenv("HOOK_SIG_OPTION_TEST", "s3cret")
    c = CapturingClient([200])
    sent = deliver_alerts(
        [_alert()],
        [{"url": "https://hooks.example/json", "format": "json",
          "secret_env": "HOOK_SIG_OPTION_TEST", "signature": "hub"}],
        client=c, sleep=NoSleep(),
    )
    assert sent == 1
    headers = c.calls[0]["headers"]
    assert "X-Signature" not in headers
    assert headers["X-Hub-Signature-256"] == _expected_signature("s3cret", c.calls[0]["body"])


# ---------------------------------------------------------------------------
# Config: ALERT_WEBHOOKS_JSON parsing
# ---------------------------------------------------------------------------


def _load_cfg(tmp_path, monkeypatch):
    monkeypatch.delenv("SIGNALS_CONTACT_EMAIL", raising=False)
    monkeypatch.delenv("SIGNALS_DB_PATH", raising=False)
    env_path = tmp_path / "empty.env"
    env_path.write_text("", encoding="utf-8")
    return Config.load(env_path=env_path)


def test_config_alert_webhooks_json_valid_list(tmp_path, monkeypatch):
    raw = json.dumps(
        [
            {"url": "https://example.com/hook", "format": "json", "secret_env": "HOOK_ONE"},
            {"url": "https://hooks.example/slack", "format": "slack"},
        ]
    )
    monkeypatch.setenv("ALERT_WEBHOOKS_JSON", raw)
    cfg = _load_cfg(tmp_path, monkeypatch)
    assert cfg.alert_webhooks == [
        {"url": "https://example.com/hook", "format": "json", "secret_env": "HOOK_ONE"},
        {"url": "https://hooks.example/slack", "format": "slack"},
    ]


def test_config_alert_webhooks_json_signature_key_passthrough(tmp_path, monkeypatch):
    """The signature option rides the same per-webhook dict as format/secret_env
    (ALERT_WEBHOOKS_JSON entries pass through verbatim, no key whitelist)."""
    raw = json.dumps(
        [
            {"url": "https://example.com/hook", "format": "json",
             "secret_env": "HOOK_ONE", "signature": "stripe"},
        ]
    )
    monkeypatch.setenv("ALERT_WEBHOOKS_JSON", raw)
    cfg = _load_cfg(tmp_path, monkeypatch)
    assert cfg.alert_webhooks[0]["signature"] == "stripe"


def test_config_alert_webhooks_default_empty(tmp_path, monkeypatch):
    monkeypatch.delenv("ALERT_WEBHOOKS_JSON", raising=False)
    cfg = _load_cfg(tmp_path, monkeypatch)
    assert cfg.alert_webhooks == []


def test_config_alert_webhooks_json_invalid_json_empty_and_warns(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("ALERT_WEBHOOKS_JSON", "{not valid json")
    bridge = LoguruCaplog(caplog)
    cfg = None
    try:
        with bridge:
            cfg = _load_cfg(tmp_path, monkeypatch)
    except Exception:
        bridge.__exit__(None, None, None)
        raise
    assert cfg.alert_webhooks == []
    assert any("ALERT_WEBHOOKS_JSON" in m for m in bridge.messages())


def test_config_alert_webhooks_json_non_array_empty_and_warns(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("ALERT_WEBHOOKS_JSON", '{"url": "https://example.com/hook"}')
    bridge = LoguruCaplog(caplog)
    cfg = None
    try:
        with bridge:
            cfg = _load_cfg(tmp_path, monkeypatch)
    except Exception:
        bridge.__exit__(None, None, None)
        raise
    assert cfg.alert_webhooks == []
    assert any("ALERT_WEBHOOKS_JSON" in m for m in bridge.messages())


def test_config_legacy_alert_webhook_url_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://legacy.example/hook")
    cfg = _load_cfg(tmp_path, monkeypatch)
    assert cfg.alert_webhook_url == "https://legacy.example/hook"
    assert cfg.alert_webhooks == []
