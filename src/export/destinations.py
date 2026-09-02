"""Config-driven export destination plugins (Plan Task 14).

A small Destination interface so sinks are config-only. FileDestination writes
via the existing export path conventions (write_jsonl for alerts, plain text
for digest strings); WebhookDestination REUSES deliver_alerts/post_json_webhook
(DRY — no parallel delivery system).

Configured via ``exports.destinations`` in config/default.yaml:
    exports:
      destinations:
        - {type: file}
        - {type: webhook}   # uses config.alert_webhooks + config.alert_routes
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from loguru import logger

from src.export.alerts import _NO_CLIENT, Alert, deliver_alerts, write_jsonl


class Destination(Protocol):
    """A sink that can deliver export items (Alerts or a digest text string)."""

    def deliver(self, items, config) -> str:
        """Deliver ``items``; returns a short description of the outcome."""
        ...


class FileDestination:
    """Writes alerts as JSONL (or a digest string as text) under storage dirs.

    APPEND semantics throughout (unified with the alerts path — see
    ``write_jsonl``): both the digest-text branch and the plain-records branch
    append to the existing file rather than overwriting it, so repeated
    deliveries accumulate instead of clobbering history.
    """

    def __init__(self, *, filename: str = "alerts.jsonl"):
        self.filename = filename

    def deliver(self, items, config) -> str:
        base = Path(getattr(config.storage, "alerts_dir", "data/alerts"))
        base.mkdir(parents=True, exist_ok=True)
        path = base / self.filename
        if isinstance(items, str):
            # Append (was: overwrite) so digest re-runs keep prior content.
            with path.open("a", encoding="utf-8") as fh:
                fh.write(items if items.endswith("\n") else items + "\n")
        elif items and isinstance(items[0], Alert):
            write_jsonl(list(items), str(path))
        else:
            # Plain records (e.g. dicts or pre-serialized lines): JSONL/text.
            lines = [
                item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
                for item in items
            ]
            with path.open("a", encoding="utf-8") as fh:
                for line in lines:
                    fh.write(line + "\n")
        n = len(items) if hasattr(items, "__len__") else 1
        logger.info("file destination wrote {} item(s) -> {}", n, path)
        return str(path)


class WebhookDestination:
    """Delivers alerts through the existing (signed) webhook delivery path.

    DRY: the actual POSTs, HMAC signing, retries, and dedupe all live in
    src/export/alerts.py (deliver_alerts/post_json_webhook) — this class only
    maps config to that entry point.
    """

    def __init__(
        self,
        *,
        webhooks: list[dict] | None = None,
        routes: list[dict] | None = None,
        client=None,  # None → deliver_alerts creates its own HTTP client
    ):
        self.webhooks = webhooks
        self.routes = routes
        self._own_client = client

    def deliver(self, items, config) -> str:
        webhooks = self.webhooks if self.webhooks is not None else config.alert_webhooks
        routes = self.routes if self.routes is not None else config.alert_routes
        sent = deliver_alerts(
            list(items), webhooks, routes=routes or None,
            client=self._own_client if self._own_client is not None else _NO_CLIENT,
            timeout=float(getattr(config, "alert_webhook_timeout_s", 10.0)),
        )
        logger.info("webhook destination delivered {} alert(s)", sent)
        return f"webhook:{sent}"


def load_destinations(config) -> list[Destination]:
    """Instantiate destinations from config.exports.destinations.

    Unknown types are skipped with a warning. Empty list → default file
    destination (pre-plugin behavior).
    """
    specs = getattr(getattr(config, "exports", None), "destinations", None) or [{"type": "file"}]
    out: list[Destination] = []
    for spec in specs:
        if isinstance(spec, str):
            spec = {"type": spec}
        if not isinstance(spec, dict):
            # Defensive: malformed spec (number, list, None ...) is skipped,
            # never a crash.
            logger.warning("export destination skipped: non-dict spec {!r}", spec)
            continue
        dtype = (spec or {}).get("type")
        if dtype == "file":
            kwargs = {k: v for k, v in spec.items() if k in ("filename",)}
            out.append(FileDestination(**kwargs))
        elif dtype == "webhook":
            kwargs = {k: v for k, v in spec.items() if k in ("webhooks", "routes")}
            out.append(WebhookDestination(**kwargs))
        else:
            logger.warning("export destination skipped: unknown type {!r}", dtype)
    if not out:
        out.append(FileDestination())
    return out
