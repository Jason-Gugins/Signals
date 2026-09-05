"""DNS evidence collection. parse_spf and match helpers are PURE."""

from __future__ import annotations

from dataclasses import dataclass, field

from src.sources.techstack.fingerprint import TechMatch, match_dns


@dataclass
class DnsEvidence:
    mx: list[str] = field(default_factory=list)
    txt: list[str] = field(default_factory=list)
    spf_includes: list[str] = field(default_factory=list)
    cname: dict[str, str] = field(default_factory=dict)
    ns: list[str] = field(default_factory=list)


def parse_spf(txt_records: list[str]) -> list[str]:
    out = []
    for rec in txt_records:
        s = rec.strip().strip('"')
        if not s.lower().startswith("v=spf1"):
            continue
        for tok in s.split():
            if tok.lower().startswith("include:"):
                out.append(tok.split(":", 1)[1])
    return out


def probe_dns(domain: str, *, resolver=None, subdomains=("www", "info", "mail", "status", "careers", "jobs", "help", "docs", "go", "try")) -> DnsEvidence:
    ev = DnsEvidence()
    if resolver is None:
        try:
            import dns.resolver
            resolver = dns.resolver.Resolver()
            resolver.lifetime = 3
        except Exception:
            from loguru import logger

            logger.debug("dnspython unavailable; DNS probe skipped for remaining collects")
            return ev

    def _q(name, rtype):
        try:
            return resolver.resolve(name, rtype)
        except Exception:
            return []

    for ans in _q(domain, "MX"):
        ev.mx.append(str(getattr(ans, "exchange", ans)).rstrip(".").lower())
    for ans in _q(domain, "TXT"):
        ev.txt.append(str(ans).strip('"'))
    ev.spf_includes = parse_spf(ev.txt)
    for ans in _q(domain, "NS"):
        ev.ns.append(str(ans).rstrip(".").lower())
    for sub in subdomains:
        host = f"{sub}.{domain}"
        for ans in _q(host, "CNAME"):
            ev.cname[host] = str(ans).rstrip(".").lower()
            break
    return ev


def statuspage_target(domain: str, *, resolver=None) -> str | None:
    """ONE CNAME query: return the *.statuspage.io host status.<domain> points at.

    Fail-open contract for the plan() gate: any resolver failure (NXDOMAIN,
    timeout, no dnspython) returns None — a DNS probe problem must never break
    planning. Returns the lowercased, trailing-dot-stripped target only when it
    ends with "statuspage.io"; the raw CNAME is otherwise discarded, so this is
    the hook the status-page poller needs (plan verified 2026-09-04).
    """
    if resolver is None:
        try:
            import dns.resolver

            resolver = dns.resolver.Resolver()
            resolver.lifetime = 3
        except Exception:
            return None
    try:
        answers = resolver.resolve(f"status.{domain}", "CNAME")
    except Exception:
        return None
    for ans in answers:
        target = str(ans).rstrip(".").lower()
        if target.endswith("statuspage.io"):
            return target
    return None


def dns_evidence_to_matches(ev: DnsEvidence, rules: dict) -> list:
    return match_dns(ev, rules)
