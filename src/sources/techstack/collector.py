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


# Single DNS probe per (domain, collect-day): the runner calls BOTH parse()
# and harvest_tech() for the same html fetch, and both need the DnsEvidence
# (parse for the snapshot/delta, harvest for vendor matching). Keyed by
# domain+today and pruned to the current day so memory stays bounded; tests
# clear it per-case via tests/test_dns_delta.py's autouse fixture.
_DNS_CACHE: dict = {}


def probe_dns_cached(domain: str, today: str):
    """probe_dns result memoized per (domain, today) so ONE probe serves the
    whole collect. Resolves dns_probe.probe_dns lazily so monkeypatching the
    probe module keeps working."""
    key = (str(domain), str(today))
    ev = _DNS_CACHE.get(key)
    if ev is not None:
        return ev
    from src.sources.techstack.dns_probe import probe_dns

    ev = probe_dns(domain)
    for k in [k for k in _DNS_CACHE if k[1] != str(today)]:
        _DNS_CACHE.pop(k, None)
    _DNS_CACHE[key] = ev
    return ev


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
        cands = tech_to_candidates(
            account.domain,
            [m.vendor for m in named],
            [],
            [],
            rules,
            list(rules.get("competitors") or []),
            today=today,
        )
        if kind in ("", "html"):
            # DNS raw delta (Task 14): the probe already runs for html tasks;
            # snapshot its DnsEvidence on the account and diff spf_includes
            # cycle-over-cycle. Candidates are parse's return type, so the
            # emission rides here rather than on harvest_tech (which returns
            # TechMatch objects, not candidates).
            cands = list(cands) + self._dns_delta(account, task_meta, rules, today)
        return cands

    def _dns_delta(self, account, task_meta, rules, today):
        """Snapshot DnsEvidence into extra_data['dns_evidence'] and emit
        tech_churn for spf_includes that disappeared since the prior snapshot.

        Contract (mirrors the BBB rating baseline): the snapshot is persisted
        through the runner-injected AccountRegistry — registry.get(domain) ->
        MERGE into extra_data -> registry.upsert — so unrelated keys survive.
        No registry in task_meta -> nothing. A totally empty probe result
        (dnspython missing, total DNS failure) persists and emits NOTHING: a
        probe failure must never masquerade as mail-vendor churn. Includes
        claimed by any fingerprint rule's spf_include needles are skipped —
        those vendors already have their own diff. today is a date built from
        task_meta (no clock reads — purity).
        """
        registry = (task_meta or {}).get("registry")
        if registry is None:
            return []
        today_iso = today.isoformat()
        try:
            ev = probe_dns_cached(account.domain, today_iso)
        except Exception:
            from loguru import logger

            logger.exception("dns probe failed for {}", account.domain)
            return []
        # Probe produced nothing at all -> DNS unavailable; never treat that
        # as "all includes removed".
        if not (ev.mx or ev.txt or ev.spf_includes or ev.cname or ev.ns):
            return []
        snapshot = {
            "observed_at": today_iso,
            "mx": list(ev.mx),
            "spf_includes": list(ev.spf_includes),
            "cname": dict(ev.cname),
        }

        base = registry.get(account.domain) or account
        original = dict(getattr(base, "extra_data", None) or {})
        prior = original.get("dns_evidence") or {}

        removed: list[str] = []
        if prior:
            current = set(ev.spf_includes)
            removed = [i for i in (prior.get("spf_includes") or []) if i not in current]

        claimed = set()
        for spec in (rules.get("vendors") or rules).values():
            match = (spec or {}).get("match") or {}
            claimed.update(str(n).casefold() for n in match.get("spf_include") or [])
        iso_month = today_iso[:7]
        out = []
        for inc in removed:
            if str(inc).casefold() in claimed:
                continue
            out.append(
                SignalCandidate(
                    "tech_churn",
                    today_iso,
                    f"dnschurn:{account.domain}:{inc}:{iso_month}",
                    title=f"SPF include removed: {inc}",
                    confidence=0.5,
                    evidence_data={
                        "spf_include_removed": inc,
                        "kind": "mail_vendor_switch",
                    },
                )
            )

        if prior != snapshot:
            stored = dict(original)
            stored["dns_evidence"] = snapshot
            base.extra_data = stored
            registry.upsert(base)
        return out

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
                from src.sources.techstack.dns_probe import dns_evidence_to_matches
                from src.sources.techstack.fingerprint import merge_matches

                # Shared (domain, today)-keyed probe: parse() already probed
                # for the dns_evidence snapshot — reuse it instead of paying
                # ~12 DNS queries a second time per collect.
                dns_ev = probe_dns_cached(
                    account.domain, str((task_meta or {}).get("today") or "")
                )
                matches = merge_matches(matches, dns_evidence_to_matches(dns_ev, rules))
            except Exception:
                # Fail-open, but diagnosable: a silently broken probe would be
                # indistinguishable from "no DNS evidence for this domain".
                from loguru import logger

                logger.exception("dns probe merge failed for {}", account.domain)
        return matches
