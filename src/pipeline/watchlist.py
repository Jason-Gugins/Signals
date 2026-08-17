"""Closed-lost watchlist and re-trigger alerts."""

from __future__ import annotations

import csv
import json
from datetime import date, timedelta

from src.core.db import Database
from src.core.models import Account
from src.export.alerts import Alert
from src.identity.domains import root_domain
from src.identity.registry import AccountRegistry


def add(db: Database, domain: str, *, reason: str, notes: str | None = None, alert_on_types: list[str] | None = None) -> None:
    domain = root_domain(domain) or domain.casefold()
    db.upsert(
        "watchlist",
        {
            "domain": domain,
            "reason": reason,
            "notes": notes,
            "added_at": date(2026, 8, 1).isoformat(),
            "last_alert_at": None,
            "alert_on_types": json.dumps(alert_on_types) if alert_on_types else None,
        },
        pk="domain",
    )


def remove(db: Database, domain: str) -> None:
    domain = root_domain(domain) or domain.casefold()
    db.execute("DELETE FROM watchlist WHERE domain = ?", (domain,))


def check(db: Database, *, taxonomy, today: date, cooldown_days: int = 30) -> list[Alert]:
    primary = taxonomy.primary_types()
    out = []
    for row in db.query("SELECT * FROM watchlist"):
        last = row.get("last_alert_at") or row.get("added_at") or "1970-01-01"
        if row.get("last_alert_at"):
            try:
                last_d = date.fromisoformat(str(row["last_alert_at"])[:10])
                if (today - last_d).days < cooldown_days:
                    continue
            except ValueError:
                pass
        types = json.loads(row["alert_on_types"]) if row.get("alert_on_types") else None
        sigs = db.query(
            "SELECT * FROM signals WHERE domain=? AND first_seen_at > ? ORDER BY first_seen_at",
            (row["domain"], last),
        )
        hit = None
        for s in sigs:
            if types and s["signal_type"] not in types:
                continue
            if not types and s["signal_type"] not in primary:
                continue
            hit = s
            break
        if not hit:
            continue
        acct = db.one("SELECT * FROM accounts WHERE domain=?", (row["domain"],)) or {}
        out.append(
            Alert(
                domain=row["domain"],
                company=acct.get("name") or row["domain"],
                tier=int(acct.get("tier") or 4),
                score=float(acct.get("score") or 0),
                signal_type=hit["signal_type"],
                evidence=hit.get("evidence") or "",
                url=hit.get("url"),
                play=None,
                urgency=6,
                at=hit["first_seen_at"],
            )
        )
        db.upsert(
            "watchlist",
            {**dict(row), "last_alert_at": today.isoformat()},
            pk="domain",
            overwrite={"last_alert_at"},
        )
    return out


def import_closed_lost(db: Database, csv_path: str) -> int:
    registry = AccountRegistry(db)
    n = 0
    with open(csv_path, encoding="utf-8", newline="") as fh:
        for raw in csv.DictReader(fh):
            rec = {(k or "").strip().casefold(): (v or "").strip() for k, v in raw.items()}
            domain = root_domain(rec.get("domain"))
            if not domain:
                continue
            if registry.get(domain) is None:
                registry.upsert(Account(domain=domain, name=rec.get("name") or domain, seed_source="watchlist"))
            add(db, domain, reason="closed_lost", notes=rec.get("notes") or rec.get("reason"))
            n += 1
    return n
