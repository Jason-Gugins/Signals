"""Alert stream and Slack-compatible webhook delivery."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from loguru import logger


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


def post_webhook(alerts: list[Alert], url: str, *, client=None, batch: int = 10) -> int:
    import httpx

    own = client is None
    client = client or httpx.Client(timeout=10)
    sent = 0
    try:
        for i in range(0, len(alerts), batch):
            chunk = alerts[i : i + batch]
            try:
                for a in chunk:
                    resp = client.post(url, json={"text": format_slack_text(a)})
                    if getattr(resp, "status_code", 200) >= 400:
                        logger.warning("webhook {} -> {}", url, getattr(resp, "status_code", "?"))
                        continue
                    sent += 1
            except Exception as exc:
                logger.warning("webhook failed: {}", exc)
        return sent
    finally:
        if own:
            client.close()
