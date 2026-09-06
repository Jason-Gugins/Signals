"""HTTP evidence extraction + fingerprint rule engine. PURE."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from html.parser import HTMLParser
import re
from urllib.parse import urlsplit, urlunsplit

from src.core.textutil import clean_text


@dataclass
class HttpEvidence:
    url: str
    headers: dict[str, str]
    cookies: list[str]
    script_srcs: list[str]
    link_hrefs: list[str]
    meta: dict[str, str]
    inline_globals: list[str]
    text_sample: str


@dataclass(frozen=True)
class TechMatch:
    vendor: str
    display: str
    category: list[str]
    tier: str
    evidence: str
    confidence: float


@dataclass
class NetworkEvidence:
    page_url: str
    hosts: tuple[str, ...]
    urls: tuple[str, ...]


def extract_network_evidence(body: bytes) -> NetworkEvidence:
    raw = json.loads(body)
    reqs = raw.get("requests") or []
    hosts, urls = [], []
    for r in reqs[:80]:
        u = (r.get("url") or "").strip()
        parts = urlsplit(u)
        if parts.scheme in {"data", "blob"}:
            continue
        if u.startswith("data:") or u.startswith("blob:"):
            continue
        if u:
            u = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
            urls.append(u)
        h = (r.get("host") or parts.hostname or "").strip().casefold()
        if h:
            hosts.append(h)
    return NetworkEvidence(
        page_url=raw.get("page_url") or "",
        hosts=tuple(dict.fromkeys(hosts)),
        urls=tuple(urls),
    )


def first_party_suffix(domain: str) -> str:
    return (domain or "").casefold().lstrip(".")


def is_first_party(host: str, domain: str) -> bool:
    h, d = host.casefold(), first_party_suffix(domain)
    return bool(d) and (h == d or h.endswith("." + d))


def observed_hosts(ev, *, domain: str) -> tuple[str, ...]:
    hosts = list(getattr(ev, "hosts", ()) or [])
    for src in getattr(ev, "script_srcs", []) or []:
        h = (urlsplit(src).hostname or "").casefold()
        if h:
            hosts.append(h)
    out = []
    for h in hosts:
        if not h or is_first_party(h, domain):
            continue
        if h not in out:
            out.append(h)
    return tuple(out)


def dynamic_matches(ev, *, domain: str) -> list[TechMatch]:
    out = []
    for h in observed_hosts(ev, domain=domain):
        out.append(
            TechMatch(
                vendor=f"host:{h}",
                display=h,
                category=["observed"],
                tier="unknown",
                evidence="network_host",
                confidence=0.4,
            )
        )
    return out


class _Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts: list[str] = []
        self.links: list[str] = []
        self.meta: dict[str, str] = [] if False else {}
        self._in_script = False
        self.inline: list[str] = []

    def handle_starttag(self, tag, attrs):
        ad = dict(attrs)
        if tag == "script":
            if ad.get("src"):
                self.scripts.append(ad["src"])
            else:
                self._in_script = True
        elif tag == "link" and ad.get("href"):
            self.links.append(ad["href"])
        elif tag == "meta":
            key = ad.get("name") or ad.get("property")
            if key:
                self.meta[key] = ad.get("content") or ""

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script = False

    def handle_data(self, data):
        if self._in_script:
            self.inline.append(data)


_GLOBAL = re.compile(r"window\.(\w{3,40})\s*=")
_INITS = ["analytics.load(", "hbspt.forms.create(", "Munchkin.init("]


def extract_http_evidence(body: bytes, headers: dict, url: str) -> HttpEvidence:
    html = body.decode("utf-8", "replace")
    p = _Page()
    try:
        p.feed(html)
    except Exception:
        pass
    cookies = []
    set_cookie = headers.get("set-cookie") or headers.get("Set-Cookie") or ""
    if set_cookie:
        cookies = [c.split("=", 1)[0].strip() for c in set_cookie.split(",") if "=" in c]
    inline_src = "\n".join(p.inline)
    globals_ = _GLOBAL.findall(inline_src)
    for init in _INITS:
        if init in inline_src:
            globals_.append(init.rstrip("("))
    text = clean_text(re.sub(r"<[^>]+>", " ", html)) or ""
    return HttpEvidence(
        url=url,
        headers={k.lower(): v for k, v in headers.items()},
        cookies=cookies,
        script_srcs=p.scripts,
        link_hrefs=p.links,
        meta=p.meta,
        inline_globals=globals_,
        text_sample=text[:2000],
    )


def _rules_vendors(rules: dict) -> dict:
    return rules.get("vendors") or rules


def _host_hit(host: str, needle: str) -> bool:
    h, n = host.casefold(), needle.casefold()
    return h == n or h.endswith("." + n)


def _header_hit(headers, spec: dict) -> bool:
    """True when ANY (key, value) pair in the rule's ``header`` match spec is
    present in the response headers with a casefolded SUBSTRING value match.

    Header keys are matched case-insensitively (``extract_http_evidence``
    lowercases them; this lookup re-lowercases defensively for synthetic
    evidence objects). An evidence object without a ``headers`` attribute or
    an empty one can never hit.
    """
    if not headers or not spec:
        return False
    low = {str(k).lower(): v for k, v in headers.items()}
    for key, needle in spec.items():
        actual = low.get(str(key).lower())
        if actual is None:
            continue
        if str(needle).casefold() in str(actual).casefold():
            return True
    return False


def is_challenge_evidence(ev, *, status: int | None = None) -> bool:
    if status == 403:
        return True
    for h in getattr(ev, "hosts", ()) or []:
        if _host_hit(h, "challenges.cloudflare.com"):
            return True
    return False


_JS_MARKERS = (b"Just a moment", b"Checking your browser", b"cf-browser-verification")
_MANAGED_MARKERS = (b"Press &amp; Hold", b"Press & Hold", b"cf-turnstile")
_CHALLENGE_HOST = "challenges.cloudflare.com"


def classify_cloudflare_challenge(
    *,
    status: int | None = None,
    body: bytes = b"",
    challenge_host: str | None = None,
) -> str | None:
    """Return 'js', 'managed', or None for a techstack fetch result.

    'js'     = standard JS challenge (real Chromium auto-solves)
    'managed' = Turnstile / interactive challenge (needs solver or headed)
    """
    is_cf = False
    if status == 403:
        is_cf = True
    if challenge_host and _host_hit(challenge_host, _CHALLENGE_HOST):
        is_cf = True
    low = body.lower()
    if any(m.lower() in low for m in _JS_MARKERS):
        return "js"
    if any(m.lower() in low for m in _MANAGED_MARKERS):
        return "managed"
    if challenge_host and _host_hit(challenge_host, _CHALLENGE_HOST):
        return "js"
    return "managed" if is_cf else None


def match_fingerprints(ev, rules: dict) -> list[TechMatch]:
    vendors = _rules_vendors(rules)
    hits: list[TechMatch] = []
    script_blob = " ".join(getattr(ev, "script_srcs", []) or [])
    cnames = " ".join((getattr(ev, "cname", {}) or {}).values())
    mx = " ".join(getattr(ev, "mx", []) or [])
    spf = " ".join(getattr(ev, "spf_includes", []) or [])
    job_text = getattr(ev, "text_sample", "") or ""
    for key, spec in vendors.items():
        match = spec.get("match") or {}
        evidence = None
        if any(s in script_blob for s in match.get("script_src") or []):
            evidence = "script_src"
        if any(s in cnames for s in match.get("dns_cname") or []):
            evidence = evidence or "dns_cname"
        if any(s in spf for s in match.get("spf_include") or []):
            evidence = evidence or "spf_include"
        if any(s in mx for s in match.get("mx") or []):
            evidence = evidence or "mx"
        if any(s.casefold() in job_text.casefold() for s in match.get("job_text") or []):
            evidence = evidence or "job_text"
        needles = match.get("network_host") or []
        if any(_host_hit(h, s) for h in (getattr(ev, "hosts", ()) or ()) for s in needles):
            evidence = evidence or "network_host"
        # Response-header evidence (Task 14): HttpEvidence carries the fetch's
        # response headers; DNS/network evidence objects have none.
        if _header_hit(getattr(ev, "headers", None), match.get("header") or {}):
            evidence = evidence or "header"
        if evidence:
            cat = spec.get("category") or []
            if isinstance(cat, str):
                cat = [cat]
            hits.append(
                TechMatch(
                    vendor=key,
                    display=spec.get("display") or key,
                    category=list(cat),
                    tier=spec.get("tier") or "mid",
                    evidence=evidence,
                    confidence=0.8 if evidence in {"script_src", "mx", "network_host", "header"} else 0.7,
                )
            )
    return hits


def match_dns(ev, rules: dict) -> list[TechMatch]:
    return match_fingerprints(ev, rules)


def merge_matches(*groups: list[TechMatch]) -> list[TechMatch]:
    best: dict[str, TechMatch] = {}
    for group in groups:
        for m in group:
            prev = best.get(m.vendor)
            if prev is None or m.confidence > prev.confidence:
                best[m.vendor] = m
    return [best[k] for k in sorted(best)]


def promote_or_observe(ev, rules: dict, *, domain: str) -> list[TechMatch]:
    named = match_fingerprints(ev, rules)
    named_keys = {m.vendor for m in named}
    needles: list[str] = []
    for key, spec in _rules_vendors(rules).items():
        if key not in named_keys:
            continue
        match = (spec or {}).get("match") or {}
        needles.extend(match.get("network_host") or [])
    claimed = {h for h in observed_hosts(ev, domain=domain) if any(_host_hit(h, n) for n in needles)}
    dyn = [m for m in dynamic_matches(ev, domain=domain) if m.display not in claimed]
    return merge_matches(named, dyn)


def load_fingerprint_rules() -> dict:
    import yaml
    from pathlib import Path

    path = Path(__file__).resolve().parents[3] / "config" / "fingerprints.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))
