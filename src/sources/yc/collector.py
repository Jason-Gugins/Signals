"""YC batch-directory collector (STUB — Inertia data-page parser).

SPIKE VERDICT (Task 6, data/probe/P2_SOURCE_SPIKE.md §2, 2026-08-31):
https://www.ycombinator.com/companies?batch=<batch> returns a client-rendered
Inertia.js shell — a ``div[data-page]`` mount point whose JSON props carry only
``env`` and ``currentBatch`` (identical props on the X-Inertia XHR). Companies
load via further async calls and are NOT extractable server-side; a live
collector needs a browser-tier upgrade. This module ships the pure parser for
the ``data-page`` shape (exercised by a synthetic fixture) and a disabled-by-
default adapter; wiring happens later.
"""

from __future__ import annotations

import html
import json
import re
from datetime import date

from src.core.models import Account
from src.identity.names import normalize_name
from src.sources.base import FetchTask, SignalCandidate, SourceAdapter
from src.sources.registry import register

YC_BATCH_URL = "https://www.ycombinator.com/companies?batch={batch}"

_DATA_PAGE_RE = re.compile(r"data-page\s*=\s*\"([^\"]+)\"")


def parse_yc_batch(html_text: str) -> list[dict]:
    """PURE. Extract the companies list from an Inertia ``div[data-page]`` shell.

    Finds the ``data-page`` attribute, HTML-unescapes it (``&quot;`` etc.),
    parses the JSON, and returns ``page["props"]["companies"]`` when present.
    The real current shape per the spike carries only ``env``/``currentBatch``
    in props — in that case (or on any parse failure) returns ``[]``.
    """
    match = _DATA_PAGE_RE.search(html_text)
    if not match:
        return []
    try:
        page = json.loads(html.unescape(match.group(1)))
    except (ValueError, TypeError):
        return []
    props = page.get("props") if isinstance(page, dict) else None
    if not isinstance(props, dict):
        return []
    companies = props.get("companies")
    return companies if isinstance(companies, list) else []


def yc_to_candidates(companies: list[dict], account: Account, *, today: date) -> list[SignalCandidate]:
    """PURE. Companies -> identity-evidence candidates.

    Emits one ``intent_3rd_topic`` candidate per company entry that matches the
    account (natural key ``yc:<company_slug>``). When the companies list is
    empty — the real current spike shape — returns ``[]``.
    """
    want = normalize_name(account.name) if account.name else ""
    out: list[SignalCandidate] = []
    for c in companies:
        if not isinstance(c, dict):
            continue
        name = c.get("name") or ""
        if want and want not in (normalize_name(name) or ""):
            continue
        slug = c.get("slug") or ""
        if not slug:
            continue
        out.append(
            SignalCandidate(
                signal_type="intent_3rd_topic",
                observed_at=today.isoformat(),
                natural_key=f"yc:{slug}",
                title=name,
                url=f"https://www.ycombinator.com/companies/{slug}",
                confidence=0.6,
                evidence_data={
                    "batch": c.get("batch"),
                    "yc_slug": slug,
                    "identity_evidence": True,
                },
            )
        )
    return out


@register
class YcBatchSource(SourceAdapter):
    """YC batch directory adapter (STUB, disabled by default).

    Spike finding: the batch page is an Inertia.js shell — companies load via
    further async calls, so this http-tier parser needs a browser-tier upgrade
    to be live. Not wired into config/sources.yaml yet.
    """

    key = "yc_batch"
    tier = "http"
    cadence_hours = 720

    def plan(self, account: Account, cursor):
        batch = (account.extra_data or {}).get("yc_batch") or "S24"
        return [
            FetchTask(
                source=self.key,
                url=YC_BATCH_URL.format(batch=batch),
                domain=account.domain,
                meta={"batch": batch},
            )
        ]

    def parse(self, doc, account: Account, task_meta: dict) -> list[SignalCandidate]:
        if not doc.body:
            return []
        today = date.fromisoformat(task_meta["today"])
        return yc_to_candidates(parse_yc_batch(doc.body.decode("utf-8", "replace")), account, today=today)
