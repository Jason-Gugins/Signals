"""Alert stream and Slack-compatible webhook delivery."""

from __future__ import annotations

import json
import os
import random
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

# Module-level indirection so tests can patch the HTTP entry point.
_urlopen = urlopen

from loguru import logger

# Default HTTP timeout for webhook posts (seconds).
DEFAULT_WEBHOOK_TIMEOUT_S = 10
# Retry policy: 3 attempts with exponential backoff + jitter.
WEBHOOK_ATTEMPTS = 3
WEBHOOK_BASE_DELAY_S = 1.0
WEBHOOK_JITTER = 0.2

# In-memory natural-key dedupe store: natural_key -> delivery epoch seconds.
# Deliberately process-local (no persistence); a restart clears the window.
_DELIVERED: dict[tuple, float] = {}


def _validate_webhook_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


@dataclass
class Alert:
    domain: str
    company: str
    tier: int
    score: float
    signal_type: str
    evidence: str
    url: str | None
    play: str | None
    urgency: int
    at: str


def build_alerts(db, *, since: str, min_tier: int = 2, primary_only: bool = True) -> list[Alert]:
    from src.signals.taxonomy import Taxonomy

    tax = Taxonomy.load()
    primary = tax.primary_types()
    rows = db.query(
        """
        SELECT s.*, a.name, a.tier, a.score
        FROM signals s JOIN accounts a ON a.domain = s.domain
        WHERE s.first_seen_at >= ?
        ORDER BY s.first_seen_at DESC
        """,
        (since,),
    )
    out = []
    for r in rows:
        if r.get("tier") is not None and int(r["tier"]) > min_tier:
            continue
        if primary_only and r["signal_type"] not in primary:
            continue
        play = db.one("SELECT play_id FROM play_assignments WHERE domain=? ORDER BY rank LIMIT 1", (r["domain"],))
        out.append(
            Alert(
                domain=r["domain"],
                company=r.get("name") or r["domain"],
                tier=int(r["tier"] or 4),
                score=float(r["score"] or 0),
                signal_type=r["signal_type"],
                evidence=r.get("evidence") or r.get("title") or "",
                url=r.get("url"),
                play=play["play_id"] if play else None,
                urgency=0,
                at=r["first_seen_at"],
            )
        )
    return out


def write_jsonl(alerts: list[Alert], path: str) -> int:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        for a in alerts:
            fh.write(json.dumps(asdict(a), ensure_ascii=False) + "\n")
    return len(alerts)


def format_slack_text(alert: Alert) -> str:
    bits = [f"*{alert.company}*", f"tier {alert.tier}", alert.signal_type, alert.evidence or ""]
    if alert.play:
        bits.append(f"play: {alert.play}")
    return " · ".join(b for b in bits if b)


def _natural_key(alert: Alert) -> tuple:
    return (alert.domain, alert.signal_type, alert.at)


def _deliver_once(client, url: str, payload: dict, timeout: float, headers: dict | None = None) -> bool:
    """Single POST attempt. Returns True on 2xx, False otherwise.

    ``headers`` carries extra headers (e.g. X-Signature). When empty, the call
    signature is identical to the legacy Slack path (fake clients in existing
    tests accept post(url, json=, timeout=) only)."""
    if client is not None:
        if headers:
            resp = client.post(url, json=payload, timeout=timeout,
                               headers={"Content-Type": "application/json", **headers})
        else:
            resp = client.post(url, json=payload, timeout=timeout)
        status = getattr(resp, "status_code", 500)
        if status >= 400:
            logger.warning("webhook {} -> HTTP {}", url, status)
            return False
        return True
    extra = {"Content-Type": "application/json"}
    if headers:
        extra.update(headers)
    req = Request(url, data=json.dumps(payload).encode("utf-8"),
                  headers=extra, method="POST")
    try:
        with _urlopen(req, timeout=timeout) as resp:
            return getattr(resp, "status", 200) < 400
    except URLError as exc:
        logger.warning("webhook {} -> {}", url, exc)
        return False


def _json_payload(alert: Alert) -> dict:
    """Generic JSON payload for arbitrary webhook consumers."""
    return {
        "type": alert.signal_type,
        "account": {
            "domain": alert.domain,
            "company": alert.company,
            "tier": alert.tier,
            "score": alert.score,
        },
        "signal": {
            "evidence": alert.evidence,
            "url": alert.url,
            "at": alert.at,
        },
        "why_now": alert.evidence,
    }


def _sign_body(signing_secret: str | None, payload: dict) -> dict:
    """Optional HMAC-SHA256 request signing: X-Signature: sha256=<lowercase hex>."""
    if not signing_secret:
        return {}
    import hashlib
    import hmac as hmac_mod

    body = json.dumps(payload).encode("utf-8")
    digest = hmac_mod.new(signing_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return {"X-Signature": "sha256=" + digest}


# Sentinel distinguishing "no client passed" from "client=None means urllib".
_NO_CLIENT = object()


def _filtered_pending(alerts, url: str, dedupe_window_h: float, now: float | None = None) -> list[Alert]:
    """Natural-key dedupe filter shared by post_webhook / post_json_webhook.
    Keyed per destination URL so one alert can fan out to multiple webhooks."""
    if now is None:
        now = time.time()
    cutoff_h = dedupe_window_h
    pending = []
    for a in alerts:
        key = (url,) + _natural_key(a)
        delivered_at = _DELIVERED.get(key)
        if delivered_at is not None and (now - delivered_at) < cutoff_h * 3600:
            logger.debug("webhook dedupe: skipping already-delivered {}", key)
            continue
        pending.append(a)
    return pending


def post_webhook(
    alerts: list[Alert],
    url: str,
    *,
    client=_NO_CLIENT,
    batch: int = 10,
    timeout: float = DEFAULT_WEBHOOK_TIMEOUT_S,
    sleep=time.sleep,
    dedupe_window_h: float = 24.0,
) -> int:
    """Deliver alerts to a Slack-compatible webhook.

    Hardened delivery:
    - URL must be http/https with a host; invalid URLs are skipped with a
      warning (collect runs are never crashed by a bad webhook config).
    - Each alert is retried up to 3 attempts with exponential backoff
      (1s/2s/4s base, ±20% jitter). Pass ``sleep`` to avoid real delays.
    - Delivery outcomes are logged (success / exhausted retries).
    - Natural-key dedupe: an alert whose (domain, signal_type, at) key was
      delivered within ``dedupe_window_h`` hours is skipped. The store is
      in-memory only (no persistence) — a restart clears the window.
    """
    if not _validate_webhook_url(url):
        logger.warning("webhook skipped: invalid URL {!r} (must be http/https with a host)", url)
        return 0

    pending = _filtered_pending(alerts, url, dedupe_window_h)

    own = client is _NO_CLIENT
    if own:
        import httpx

        http_client = httpx.Client(timeout=timeout)
    else:
        http_client = client
    sent = 0
    try:
        for i in range(0, len(pending), batch):
            chunk = pending[i : i + batch]
            for a in chunk:
                key = _natural_key(a)
                payload = {"text": format_slack_text(a)}
                for attempt in range(1, WEBHOOK_ATTEMPTS + 1):
                    try:
                        if _deliver_once(http_client, url, payload, timeout):
                            sent += 1
                            _DELIVERED[(url,) + key] = time.time()
                            logger.info("webhook delivered: {} (attempt {})", key, attempt)
                            break
                    except Exception as exc:
                        logger.warning("webhook {} attempt {} failed: {}", url, attempt, exc)
                    if attempt < WEBHOOK_ATTEMPTS:
                        delay = WEBHOOK_BASE_DELAY_S * (2 ** (attempt - 1))
                        delay *= 1 + random.uniform(-WEBHOOK_JITTER, WEBHOOK_JITTER)
                        sleep(delay)
                else:
                    logger.error("webhook delivery failed after {} attempts: {}", WEBHOOK_ATTEMPTS, key)
        return sent
    finally:
        if own:
            http_client.close()


def post_json_webhook(
    alerts: list[Alert],
    url: str,
    *,
    signing_secret: str | None = None,
    client=_NO_CLIENT,
    batch: int = 10,
    timeout: float = DEFAULT_WEBHOOK_TIMEOUT_S,
    sleep=time.sleep,
    dedupe_window_h: float = 24.0,
) -> int:
    """Deliver alerts to a generic JSON webhook with the same hardened skeleton
    as post_webhook (URL validation, 3-attempt exp backoff ±jitter, natural-key
    dedupe).

    Payload per alert: {"type", "account": {domain, company, tier, score},
    "signal": {evidence, url, at}, "why_now"}.

    Optional HMAC: when ``signing_secret`` is provided, sends
    ``X-Signature: sha256=<lowercase hex HMAC-SHA256 of the exact body bytes>``.
    """
    if not _validate_webhook_url(url):
        logger.warning("webhook skipped: invalid URL {!r} (must be http/https with a host)", url)
        return 0

    pending = _filtered_pending(alerts, url, dedupe_window_h)

    own = client is _NO_CLIENT
    if own:
        import httpx

        http_client = httpx.Client(timeout=timeout)
    else:
        http_client = client
    sent = 0
    try:
        for i in range(0, len(pending), batch):
            chunk = pending[i : i + batch]
            for a in chunk:
                key = _natural_key(a)
                payload = _json_payload(a)
                headers = _sign_body(signing_secret, payload)
                for attempt in range(1, WEBHOOK_ATTEMPTS + 1):
                    try:
                        if _deliver_once(http_client, url, payload, timeout, headers=headers):
                            sent += 1
                            _DELIVERED[(url,) + key] = time.time()
                            logger.info("webhook delivered: {} (attempt {})", key, attempt)
                            break
                    except Exception as exc:
                        logger.warning("webhook {} attempt {} failed: {}", url, attempt, exc)
                    if attempt < WEBHOOK_ATTEMPTS:
                        delay = WEBHOOK_BASE_DELAY_S * (2 ** (attempt - 1))
                        delay *= 1 + random.uniform(-WEBHOOK_JITTER, WEBHOOK_JITTER)
                        sleep(delay)
                else:
                    logger.error("webhook delivery failed after {} attempts: {}", WEBHOOK_ATTEMPTS, key)
        return sent
    finally:
        if own:
            http_client.close()


def _resolve_secret(secret_env: str | None) -> str | None:
    """Resolve a signing secret from an ENV VAR NAME at send time. Warn+skip on miss."""
    if not secret_env:
        return None
    val = os.environ.get(secret_env)
    if not val:
        logger.warning(
            "webhook skipped: secret env {!r} not set (set it to the signing secret, "
            "never put the secret itself in config)",
            secret_env,
        )
        return None
    return val


def deliver_alerts(
    alerts: list[Alert],
    webhooks: list[dict],
    *,
    client=_NO_CLIENT,
    batch: int = 10,
    timeout: float = DEFAULT_WEBHOOK_TIMEOUT_S,
    sleep=time.sleep,
    dedupe_window_h: float = 24.0,
) -> int:
    """Dispatch alerts to every configured webhook (config.alert_webhooks shape:
    list of {url, format: 'slack'|'json', secret_env?}).

    Secret resolution happens at SEND time: ``secret_env`` is an ENV VAR NAME,
    resolved via os.environ; missing env → that webhook is skipped with a warning.
    """
    sent = 0
    for wh in webhooks:
        url = wh.get("url")
        fmt = wh.get("format", "json")
        if not _validate_webhook_url(url):
            logger.warning("webhook skipped: invalid URL {!r} (must be http/https with a host)", url)
            continue
        signing_secret = _resolve_secret(wh.get("secret_env"))
        if wh.get("secret_env") and signing_secret is None:
            continue
        if fmt == "slack":
            sent += post_webhook(
                alerts, url, client=client, batch=batch, timeout=timeout,
                sleep=sleep, dedupe_window_h=dedupe_window_h,
            )
        else:
            sent += post_json_webhook(
                alerts, url, signing_secret=signing_secret, client=client,
                batch=batch, timeout=timeout, sleep=sleep,
                dedupe_window_h=dedupe_window_h,
            )
    return sent
