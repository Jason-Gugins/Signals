"""Accounts, signals, and plays CSV exports."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from src.core.db import Database


ACCOUNT_COLUMNS = [
    "domain", "name", "tier", "score", "buying_window", "urgency", "industry",
    "employee_count", "hq_country", "top_play", "top_signal_1", "top_signal_2", "top_signal_3",
    "best_contact", "best_contact_title", "linkedin_url", "last_signal_at", "signal_count",
    "sources", "icp_fit", "cohort", "scored_at",
]
SIGNAL_COLUMNS = [
    "domain", "signal_type", "category", "degree", "origin", "catalyst", "polarity",
    "observed_at", "evidence", "url", "source", "confidence", "person_key", "signal_id", "first_seen_at",
]
PLAY_COLUMNS = [
    "domain", "rank", "play_id", "play_name", "urgency", "opener", "t24", "cta",
    "contact_name", "contact_title", "contact_linkedin", "signal_type", "evidence",
]
FUNDING_COLUMNS = [
    "domain", "entity_name", "cik", "observed_at", "amount_usd", "amount_display",
    "round_stage", "industry_group", "exemption", "is_amendment", "state", "city",
    "phone", "entity_type", "issuer_size", "min_investment", "investors_already",
    "investors_new", "related_persons", "url", "accession", "confidence", "signal_id",
]


def _write(path: str, columns: list[str], rows: list[dict]) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") if row.get(k) is not None else "" for k in columns})
    return str(p)


def export_accounts(db: Database, path: str, *, cohort=None, tier_max=None) -> str:
    clauses = ["1=1"]
    params: list = []
    if cohort:
        clauses.append("cohort = ?")
        params.append(cohort)
    if tier_max is not None:
        clauses.append("(tier IS NULL OR tier <= ?)")
        params.append(tier_max)
    accounts = db.query("SELECT * FROM accounts WHERE " + " AND ".join(clauses) + " ORDER BY domain", params)
    rows = []
    for a in accounts:
        sigs = db.query(
            "SELECT signal_type, observed_at, source FROM signals WHERE domain=? ORDER BY observed_at DESC",
            (a["domain"],),
        )
        plays = db.query("SELECT play_id FROM play_assignments WHERE domain=? ORDER BY rank", (a["domain"],))
        contact = db.one("SELECT name, title, linkedin_url FROM contacts WHERE domain=? LIMIT 1", (a["domain"],))
        rows.append(
            {
                **a,
                "top_play": plays[0]["play_id"] if plays else "",
                "top_signal_1": sigs[0]["signal_type"] if len(sigs) > 0 else "",
                "top_signal_2": sigs[1]["signal_type"] if len(sigs) > 1 else "",
                "top_signal_3": sigs[2]["signal_type"] if len(sigs) > 2 else "",
                "best_contact": (contact or {}).get("name") if contact else "",
                "best_contact_title": (contact or {}).get("title") if contact else "",
                "linkedin_url": (contact or {}).get("linkedin_url") if contact else "",
                "last_signal_at": sigs[0]["observed_at"] if sigs else "",
                "signal_count": len(sigs),
                "sources": ",".join(sorted({s["source"] for s in sigs if s["source"]})),
                "urgency": "",
            }
        )
    return _write(path, ACCOUNT_COLUMNS, rows)


def export_signals(db: Database, path: str, *, cohort=None, since=None) -> str:
    sql = "SELECT s.* FROM signals s"
    clauses = ["1=1"]
    params: list = []
    if cohort:
        sql += " JOIN accounts a ON a.domain = s.domain"
        clauses.append("a.cohort = ?")
        params.append(cohort)
    if since:
        clauses.append("s.observed_at >= ?")
        params.append(since)
    rows = db.query(sql + " WHERE " + " AND ".join(clauses) + " ORDER BY s.domain, s.observed_at", params)
    return _write(path, SIGNAL_COLUMNS, [dict(r) for r in rows])


def export_plays(db: Database, path: str, *, cohort=None) -> str:
    sql = "SELECT p.* FROM play_assignments p"
    clauses = ["1=1"]
    params: list = []
    if cohort:
        sql += " JOIN accounts a ON a.domain = p.domain"
        clauses.append("a.cohort = ?")
        params.append(cohort)
    rows = db.query(sql + " WHERE " + " AND ".join(clauses) + " ORDER BY p.domain, p.rank", params)
    out = []
    for r in rows:
        rec = dict(r)
        rec.setdefault("play_name", rec.get("play_id"))
        rec.setdefault("cta", "")
        rec.setdefault("contact_name", "")
        rec.setdefault("contact_title", "")
        rec.setdefault("contact_linkedin", "")
        rec.setdefault("signal_type", "")
        rec.setdefault("evidence", "")
        out.append(rec)
    return _write(path, PLAY_COLUMNS, out)


def export_funding(db: Database, path: str, *, cohort=None, since=None) -> str:
    sql = "SELECT s.* FROM signals s"
    clauses = ["s.signal_type = 'funding_form_d'"]
    params: list = []
    if cohort:
        sql += " JOIN accounts a ON a.domain = s.domain"
        clauses.append("a.cohort = ?")
        params.append(cohort)
    if since:
        clauses.append("s.observed_at >= ?")
        params.append(since)
    rows = db.query(sql + " WHERE " + " AND ".join(clauses) + " ORDER BY s.domain, s.observed_at", params)
    out = []
    for r in rows:
        rec = dict(r)
        ev = rec.get("evidence_data")
        if isinstance(ev, str):
            try:
                ev = json.loads(ev)
            except json.JSONDecodeError:
                ev = {}
        ev = ev or {}
        people = ev.get("related_persons") or []
        if isinstance(people, list):
            people_s = "; ".join(
                f"{p.get('name', '')} ({','.join(p.get('relationship') or [])})"
                if isinstance(p, dict)
                else str(p)
                for p in people
            )
        else:
            people_s = str(people)
        out.append(
            {
                **ev,
                "domain": rec.get("domain"),
                "observed_at": rec.get("observed_at"),
                "url": rec.get("url"),
                "confidence": rec.get("confidence"),
                "signal_id": rec.get("signal_id"),
                "related_persons": people_s,
                "accession": ev.get("accession") or rec.get("natural_key") or "",
            }
        )
    return _write(path, FUNDING_COLUMNS, out)


def export_all(db: Database, export_dir: str, *, cohort=None) -> list[str]:
    root = Path(export_dir)
    return [
        export_accounts(db, str(root / "accounts.csv"), cohort=cohort),
        export_signals(db, str(root / "signals.csv"), cohort=cohort),
        export_plays(db, str(root / "plays.csv"), cohort=cohort),
    ]
