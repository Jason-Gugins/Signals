"""ATS vendor + board-token discovery. detect_ats is pure."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Optional
from urllib.parse import urljoin, urlparse

from src.core.models import Account

if TYPE_CHECKING:
    from src.core.http import HttpFetcher
    from src.identity.registry import AccountRegistry


ATS_PATTERNS: dict[str, list[re.Pattern]] = {
    "greenhouse": [
        re.compile(r"boards\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)", re.I),
        re.compile(r"job-boards\.greenhouse\.io/([a-z0-9_-]+)", re.I),
    ],
    "lever": [re.compile(r"jobs\.lever\.co/([a-z0-9_-]+)", re.I)],
    "ashby": [re.compile(r"jobs\.ashbyhq\.com/([a-z0-9_-]+)", re.I)],
    "smartrecruiters": [
        re.compile(r"careers\.smartrecruiters\.com/([A-Za-z0-9_-]+)"),
        re.compile(r"jobs\.smartrecruiters\.com/([A-Za-z0-9_-]+)"),
    ],
    "workable": [
        re.compile(r"([a-z0-9-]+)\.workable\.com", re.I),
        re.compile(r"apply\.workable\.com/([a-z0-9-]+)", re.I),
    ],
    "recruitee": [re.compile(r"([a-z0-9-]+)\.recruitee\.com", re.I)],
    "workday": [
        re.compile(
            r"([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)",
            re.I,
        )
    ],
    "bamboohr": [re.compile(r"([a-z0-9-]+)\.bamboohr\.com/(?:jobs|careers)", re.I)],
    "jazzhr": [re.compile(r"([a-z0-9-]+)\.applytojob\.com", re.I)],
    "personio": [re.compile(r"([a-z0-9-]+)\.jobs\.personio\.(?:de|com)", re.I)],
    "teamtailor": [re.compile(r"([a-z0-9-]+)\.teamtailor\.com", re.I)],
}


@dataclass
class AtsMatch:
    vendor: str
    token: str
    evidence_url: str
    confidence: float
    extra: dict = field(default_factory=dict)


class _LinkCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.strong: list[str] = []
        self.scripts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        ad = {k: v or "" for k, v in attrs}
        if tag == "iframe" and ad.get("src"):
            self.strong.append(ad["src"])
        elif tag == "a" and ad.get("href"):
            self.strong.append(ad["href"])
        elif tag == "script" and ad.get("src"):
            self.scripts.append(ad["src"])


def _workday_extra(m: re.Match) -> tuple[str, dict]:
    tenant, wd, site = m.group(1), m.group(2), m.group(3)
    return tenant, {"tenant": tenant, "wd": wd, "site": site}


def detect_ats(html: str, base_url: str) -> list[AtsMatch]:
    """PURE. Scans hrefs, iframes, script srcs, and raw text."""
    collector = _LinkCollector()
    try:
        collector.feed(html)
    except Exception:
        pass
    buckets = [
        (collector.strong, 0.95),
        (collector.scripts, 0.85),
        ([html], 0.5),
    ]
    found: dict[tuple[str, str], AtsMatch] = {}
    for urls, conf in buckets:
        for url in urls:
            for vendor, pats in ATS_PATTERNS.items():
                for pat in pats:
                    for m in pat.finditer(url):
                        extra = {}
                        if vendor == "workday" and m.lastindex and m.lastindex >= 3:
                            token, extra = _workday_extra(m)
                        else:
                            token = m.group(1)
                        key = (vendor, token.casefold())
                        evidence = url if url != html else base_url
                        if len(evidence) > 500:
                            evidence = base_url
                        prev = found.get(key)
                        if prev is None or conf > prev.confidence:
                            found[key] = AtsMatch(
                                vendor=vendor,
                                token=token,
                                evidence_url=evidence,
                                confidence=conf,
                                extra=extra,
                            )
    return sorted(found.values(), key=lambda x: (-x.confidence, x.vendor))


def careers_url_candidates(domain: str) -> list[str]:
    d = domain.casefold().removeprefix("www.")
    return [
        f"https://{d}/careers",
        f"https://{d}/jobs",
        f"https://{d}/company/careers",
        f"https://careers.{d}/",
        f"https://jobs.{d}/",
        f"https://{d}/about/careers",
        f"https://{d}/join-us",
    ]


_CAREER_HREF = re.compile(r"/(careers|jobs|join-us|company/careers)(?:/|$)", re.I)


class AtsDiscovery:
    def __init__(self, fetcher: "HttpFetcher", registry: "AccountRegistry"):
        self.fetcher = fetcher
        self.registry = registry

    def discover(self, account: Account) -> Optional[AtsMatch]:
        used = 0
        max_req = 4

        def fetch(url: str):
            nonlocal used
            if used >= max_req:
                return None
            used += 1
            from src.identity.edgar_ids import _Task

            return self.fetcher.get(_Task(source="ats_discovery", url=url, domain=account.domain))

        pages: list[tuple[str, str]] = []
        if account.careers_url:
            res = fetch(account.careers_url)
            if res and res.ok and res.doc and res.doc.body:
                pages.append((account.careers_url, res.doc.body.decode("utf-8", "replace")))
        else:
            home = f"https://{account.domain}/"
            res = fetch(home)
            if res and res.ok and res.doc and res.doc.body:
                html = res.doc.body.decode("utf-8", "replace")
                pages.append((home, html))
                hop = _first_careers_link(html, home)
                if hop:
                    res2 = fetch(hop)
                    if res2 and res2.ok and res2.doc and res2.doc.body:
                        pages.append((hop, res2.doc.body.decode("utf-8", "replace")))
            if not any(detect_ats(h, u) for u, h in pages):
                for cand in careers_url_candidates(account.domain):
                    res = fetch(cand)
                    if not res or not res.ok or not res.doc or not res.doc.body:
                        continue
                    html = res.doc.body.decode("utf-8", "replace")
                    pages.append((cand, html))
                    if detect_ats(html, cand):
                        break

        best: Optional[AtsMatch] = None
        careers_url = account.careers_url
        for url, html in pages:
            matches = detect_ats(html, url)
            if matches and (best is None or matches[0].confidence > best.confidence):
                best = matches[0]
                careers_url = url
        if best:
            token = best.token
            if best.vendor == "workday" and best.extra.get("tenant"):
                token = f"{best.extra['tenant']}/{best.extra['wd']}/{best.extra['site']}"
            account.ats_vendor = best.vendor
            account.ats_token = token
            account.careers_url = careers_url
            self.registry.upsert(account, source="ats_discovery")
        return best


def _first_careers_link(html: str, base: str) -> str | None:
    collector = _LinkCollector()
    try:
        collector.feed(html)
    except Exception:
        return None
    for href in collector.strong:
        if _CAREER_HREF.search(urlparse(href).path or href):
            return urljoin(base, href)
    return None
