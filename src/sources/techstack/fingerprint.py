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
                    confidence=0.8 if evidence in {"script_src", "mx"} else 0.7,
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


def load_fingerprint_rules() -> dict:
    import yaml
    from pathlib import Path

    path = Path(__file__).resolve().parents[3] / "config" / "fingerprints.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))
