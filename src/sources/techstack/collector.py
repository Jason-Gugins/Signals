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
        if str(v).startswith("host:"):
            continue
        out.append(SignalCandidate("tech_removed", today.isoformat(), f"tech_removed:{v}:{iso_month}", title=v, confidence=0.8, evidence_data={"vendor": v}))
    return out


@register
class TechstackSource(SourceAdapter):
    key = "techstack"
    tier = "http"
    cadence_hours = 168

    def plan(self, account, cursor):
        tasks = [
            FetchTask(source=self.key, url=f"https://{account.domain}/", domain=account.domain, meta={"kind": "html"}),
            FetchTask(
                source=self.key,
                url=f"https://{account.domain}/",
                domain=account.domain,
                meta={"kind": "network", "capture": "network"},
            ),
        ]
        # Status-page gate: only domains whose status.<domain> CNAME resolves
        # to *.statuspage.io get the extra index.json poll — normal domains
        # stay at two fetches with zero 404-stamp noise. Fail-open: a DNS
        # probe failure must never break plan(). The runner injects
        # meta["today"] at fetch time; plan() deliberately sets no today.
        try:
            from src.sources.techstack.dns_probe import statuspage_target

            target = statuspage_target(account.domain)
        except Exception:
            from loguru import logger

            logger.exception("statuspage gate failed for {}", account.domain)
            return tasks
        if target:
            tasks.append(
                FetchTask(
                    source=self.key,
                    url=f"https://{target}/index.json",
                    domain=account.domain,
                    meta={"kind": "statuspage"},
                )
            )
        return tasks

    def parse(self, doc, account, task_meta):
        import json
        from datetime import date

        from src.sources.techstack.fingerprint import (
            extract_http_evidence,
            extract_network_evidence,
            load_fingerprint_rules,
            promote_or_observe,
        )

        rules = load_fingerprint_rules()
        kind = (task_meta or {}).get("kind") or ""
        if kind == "statuspage":
            return self._parse_statuspage(doc, account, task_meta)
        body = doc.body or b""
        use_net = kind == "network" or (not kind and body.lstrip().startswith(b"{"))
        if use_net:
            try:
                ev = extract_network_evidence(body or b"{}")
            except (json.JSONDecodeError, TypeError, ValueError):
                return []
        else:
            ev = extract_http_evidence(
                body, (task_meta or {}).get("response_headers") or {}, doc.url or ""
            )
        matches = promote_or_observe(ev, rules, domain=account.domain)
        if (task_meta or {}).get("cloudflare_unsolved"):
            matches = [m for m in matches if m.vendor == "cloudflare"]
        today = date.fromisoformat(task_meta["today"])
        named = [m for m in matches if m.tier != "unknown"]
        return tech_to_candidates(
            account.domain,
            [m.vendor for m in named],
            [],
            [],
            rules,
            list(rules.get("competitors") or []),
            today=today,
        )

    def _parse_statuspage(self, doc, account, task_meta):
        """statuspage index.json -> competitor_outage candidates (PURE).

        Emit one candidate per incident whose status is not resolved/postmortem;
        natural key `outage:{domain}:{incident_id}` is idempotent across cycles.
        Scheduled maintenance is ignored for v1. Fail-open: malformed JSON or
        missing keys yield [] — a broken status page never breaks the pipeline.
        """
        import json

        try:
            data = json.loads(doc.body or b"")
        except (json.JSONDecodeError, TypeError, ValueError):
            return []
        if not isinstance(data, dict):
            return []
        today = str((task_meta or {}).get("today") or "")
        out = []
        for inc in data.get("incidents") or []:
            if not isinstance(inc, dict):
                continue
            status = str(inc.get("status") or "").lower()
            if status in {"resolved", "postmortem"}:
                continue
            inc_id = inc.get("id")
            if not inc_id:
                continue
            name = inc.get("name") or "incident"
            out.append(
                SignalCandidate(
                    "competitor_outage",
                    today,
                    f"outage:{account.domain}:{inc_id}",
                    title=name,
                    confidence=0.85,
                    evidence_data={
                        "incident": name,
                        "status": status,
                        "impact": inc.get("impact"),
                        "started_at": inc.get("started_at"),
                        "url": inc.get("shortlink"),
                    },
                )
            )
        return out

    def harvest_tech(self, doc, account, task_meta):
        import json

        from src.sources.techstack.fingerprint import (
            extract_http_evidence,
            extract_network_evidence,
            load_fingerprint_rules,
            promote_or_observe,
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
            ev = extract_http_evidence(
                body, (task_meta or {}).get("response_headers") or {}, doc.url or ""
            )
        matches = promote_or_observe(ev, rules, domain=account.domain)
        if (task_meta or {}).get("cloudflare_unsolved"):
            matches = [m for m in matches if m.vendor == "cloudflare"]
            if not matches:
                from src.sources.techstack.fingerprint import TechMatch

                matches = [
                    TechMatch(
                        vendor="cloudflare",
                        display="Cloudflare",
                        category=["cdn"],
                        tier="mid",
                        evidence="challenge_unsolved",
                        confidence=0.9,
                    )
                ]
        if kind in ("", "html"):
            try:
                from src.sources.techstack.dns_probe import dns_evidence_to_matches, probe_dns
                from src.sources.techstack.fingerprint import merge_matches

                matches = merge_matches(matches, dns_evidence_to_matches(probe_dns(account.domain), rules))
            except Exception:
                # Fail-open, but diagnosable: a silently broken probe would be
                # indistinguishable from "no DNS evidence for this domain".
                from loguru import logger

                logger.exception("dns probe merge failed for {}", account.domain)
        return matches
