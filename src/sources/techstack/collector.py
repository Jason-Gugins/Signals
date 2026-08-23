"""Upsert technologies and emit tech-change signals."""

from __future__ import annotations

from datetime import date

from src.core.db import Database
from src.sources.base import FetchTask, SignalCandidate, SourceAdapter
from src.sources.registry import register
from src.sources.techstack.fingerprint import TechMatch


def upsert_technologies(db: Database, domain: str, matches: list[TechMatch], *, now: str) -> tuple[list[str], list[str]]:
    seen = {m.vendor for m in matches}
    new = []
    for m in matches:
        existing = db.one("SELECT * FROM technologies WHERE domain=? AND vendor=?", (domain, m.vendor))
        if existing is None:
            db.upsert(
                "technologies",
                {
                    "domain": domain,
                    "vendor": m.vendor,
                    "category": ",".join(m.category),
                    "tier": m.tier,
                    "first_seen_at": now,
                    "last_seen_at": now,
                    "missing_runs": 0,
                    "evidence": m.evidence,
                    "confidence": m.confidence,
                    "source": "techstack",
                },
                pk=("domain", "vendor"),
            )
            new.append(m.vendor)
        else:
            db.execute(
                "UPDATE technologies SET last_seen_at=?, missing_runs=0, evidence=?, confidence=? WHERE domain=? AND vendor=?",
                (now, m.evidence, m.confidence, domain, m.vendor),
            )
    gone = []
    rows = db.query("SELECT vendor, missing_runs FROM technologies WHERE domain=?", (domain,))
    for r in rows:
        if r["vendor"] in seen:
            continue
        miss = int(r["missing_runs"] or 0) + 1
        db.execute(
            "UPDATE technologies SET missing_runs=? WHERE domain=? AND vendor=?",
            (miss, domain, r["vendor"]),
        )
        if miss >= 2:
            gone.append(r["vendor"])
    return new, gone


def tech_to_candidates(domain, new_vendors, gone_vendors, all_rows, rules, competitors, *, today: date) -> list[SignalCandidate]:
    iso_month = today.strftime("%Y-%m")
    vendors = (rules.get("vendors") or rules)
    out = []
    for v in new_vendors:
        out.append(SignalCandidate("tech_install_new", today.isoformat(), f"tech_install_new:{v}:{iso_month}", title=v, confidence=0.75, evidence_data={"vendor": v}))
        spec = vendors.get(v) or {}
        if spec.get("tier") == "enterprise":
            out.append(SignalCandidate("high_ticket_tech", today.isoformat(), f"high_ticket_tech:{v}:{today.year}", title=v, confidence=0.6, evidence_data={"vendor": v}))
        if v in competitors:
            out.append(SignalCandidate("competitor_detected", today.isoformat(), f"competitor_detected:{v}:{iso_month}", title=v, confidence=0.7, evidence_data={"competitor": v}))
    for v in gone_vendors:
        out.append(SignalCandidate("tech_removed", today.isoformat(), f"tech_removed:{v}:{iso_month}", title=v, confidence=0.8, evidence_data={"vendor": v}))
    return out


@register
class TechstackSource(SourceAdapter):
    key = "techstack"
    tier = "http"
    cadence_hours = 168

    def plan(self, account, cursor):
        return [
            FetchTask(source=self.key, url=f"https://{account.domain}/", domain=account.domain, meta={"kind": "html"}),
            FetchTask(
                source=self.key,
                url=f"https://{account.domain}/",
                domain=account.domain,
                meta={"kind": "network", "capture": "network"},
            ),
        ]

    def parse(self, doc, account, task_meta):
        import json
        from datetime import date

        from src.sources.techstack.fingerprint import (
            extract_http_evidence,
            extract_network_evidence,
            load_fingerprint_rules,
            match_fingerprints,
        )

        rules = load_fingerprint_rules()
        kind = (task_meta or {}).get("kind") or ""
        body = doc.body or b""
        use_net = kind == "network" or (not kind and body.lstrip().startswith(b"{"))
        if use_net:
            try:
                ev = extract_network_evidence(body or b"{}")
            except (json.JSONDecodeError, TypeError, ValueError):
                return []
        else:
            ev = extract_http_evidence(body, {}, doc.url or "")
        matches = match_fingerprints(ev, rules)
        today = date.fromisoformat(task_meta["today"])
        return tech_to_candidates(
            account.domain, [m.vendor for m in matches], [], [], rules, [], today=today
        )
