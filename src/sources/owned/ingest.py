"""Owned-property file-drop ingest. PURE except default PTR."""

from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from src.identity.domains import is_public_email_domain, root_domain
from src.sources.base import SignalCandidate


def load_owned_page_rules() -> dict:
    import yaml

    path = Path(__file__).resolve().parents[3] / "config" / "owned_pages.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


EVENT_WEIGHTS = {
    "demo_request": 1.0,
    "pricing_view": 0.9,
    "form_submit": 0.85,
    "webinar_attend": 0.7,
    "doc_download": 0.6,
    "email_click": 0.5,
    "page_view": 0.35,
    "email_open": 0.25,
}


def read_drop(path: str) -> list[dict]:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    reader = csv.DictReader(text.splitlines())
    return [{(k or "").strip().casefold(): v for k, v in row.items()} for row in reader]


def classify_page(url: str, rules: dict) -> str:
    path = (urlparse(url).path or "/").casefold()
    for cls, needles in rules.items():
        if cls in {"pricing", "demo", "product", "docs", "careers", "blog"}:
            for n in needles:
                if n.casefold() in path:
                    return cls
    if path in {"", "/"}:
        return "home"
    return "other"


def resolve_visitor(row: dict, *, ptr_lookup=None) -> tuple[str | None, str]:
    email = row.get("email") or ""
    if "@" in email:
        domain = root_domain(email)
        if domain:
            return domain, "email"
        if is_public_email_domain(email.split("@")[-1]):
            return None, "public_email"
    ip = row.get("ip")
    if ip and ptr_lookup:
        host = ptr_lookup(ip)
        d = root_domain(host)
        if d:
            return d, "ptr"
    return None, "unresolved"


def aggregate_intent(rows: list[dict], *, today: date, page_rules: dict) -> list[tuple[str, SignalCandidate]]:
    groups: dict[tuple, list] = {}
    for row in rows:
        domain, method = resolve_visitor(row)
        if not domain:
            continue
        url = row.get("page_url") or row.get("page") or ""
        cls = classify_page(url, page_rules)
        day = (row.get("timestamp") or today.isoformat())[:10]
        groups.setdefault((domain, day, cls), []).append({**row, "_url": url})
    out = []
    for (domain, day, cls), items in groups.items():
        sessions = {i.get("session_id") for i in items if i.get("session_id")}
        visits = sum(int(i.get("count") or 1) for i in items)
        boost = max(EVENT_WEIGHTS.get(i.get("event_type") or "page_view", 0.35) for i in items)
        conf = min(0.95, 0.5 + 0.1 * len(sessions) + boost * 0.1)
        pages = sorted({i.get("_url") for i in items if i.get("_url")})[:5]
        events = sorted({i.get("event_type") for i in items if i.get("event_type")})
        out.append(
            (
                domain,
                SignalCandidate(
                    signal_type="intent_1st_owned",
                    observed_at=day,
                    natural_key=f"owned:{domain}:{day}:{cls}",
                    title=f"{visits} {cls} hits",
                    confidence=conf,
                    evidence_data={
                        "visits": visits,
                        "page_class": cls,
                        "page_label": cls,
                        "top_pages": pages,
                        "events": events,
                        "weight": boost,
                    },
                ),
            )
        )
    out.sort(key=lambda x: x[1].natural_key)
    return out
