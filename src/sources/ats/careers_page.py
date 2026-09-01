"""Generic /careers HTML scrape fallback — last resort when no ATS matches.

**Gating contract (enforced in ``src/sources/registry.py``):**
``_ats_vendor_ok`` special-cases the ``ats_careers_page`` key — it passes the
vendor gate ONLY when the account's ``ats_vendor`` AND ``ats_token`` are both
empty, i.e. no real ATS board applies. This makes the fallback mutually
exclusive with every ``ats_*`` adapter (no duplicate job rows). Its
``requires = ()`` means ``plan()`` itself imposes no field gate; the vendor
special case is the gate.

``plan()`` uses ``account.careers_url`` when set, else ``https://{domain}/careers``.
"""

from __future__ import annotations

import re
from html import unescape
from typing import Optional
from urllib.parse import urljoin, urlparse

from src.core.models import Account, Document
from src.sources.base import FetchTask, SignalCandidate, SourceAdapter
from src.sources.ats.common import JobPost
from src.sources.registry import register

_JOB_HREF = re.compile(r"(?:/jobs?/|/careers?/|/join-us/|/open-roles?/|/positions?/)", re.I)


def _anchor_text(html: str) -> list[tuple[str, str]]:
    """Return (href, anchor_text) pairs.

    Regex walk: anchors with hrefs; the title comes from the anchor's
    inner text, tags stripped. Falls back to a short window when the
    closing tag is missing.
    """
    out: list[tuple[str, str]] = []
    for m in re.finditer(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>", html, re.I):
        start = m.end()
        close = html.find("</a", start)
        if close < 0:
            close = min(len(html), start + 300)
        inner = html[start:close]
        text = unescape(re.sub(r"<[^>]+>", " ", inner))
        text = re.sub(r"\s+", " ", text).strip()
        out.append((m.group(1), text))
    return out


def _slug_from_href(href: str) -> str:
    tail = href.rstrip("/").rsplit("/", 1)[-1]
    tail = re.sub(r"\?.*$", "", tail)
    tail = re.sub(r"#.*$", "", tail)
    return tail or href


def parse_careers_page(html: str, base_url: str) -> list[JobPost]:
    """PURE. Anchors whose href contains /jobs/ or /careers/ (etc.) → JobPosts.

    external_id = the last path segment (slug) of the resolved URL;
    posted_at is always None (listing pages rarely expose machine dates
    conservatively). Anchor text is the title; URLs are resolved against
    ``base_url``. Duplicates by slug collapse to the first sighting.
    """
    if not html or not base_url:
        return []
    out: dict[str, JobPost] = {}
    for href, text in _anchor_text(html):
        if not _JOB_HREF.search(href):
            continue
        if href.startswith(("#", "mailto:", "javascript:")):
            continue
        url = urljoin(base_url, href)
        if not url.startswith("http"):
            continue
        # Conservative: stay on the careers-page host — offsite job boards
        # (ATS-hosted widgets etc.) are other sources' job.
        if urlparse(url).hostname != urlparse(base_url).hostname:
            continue
        # Self-referential nav: '/careers/' and '/careers' links on the
        # careers index are navigation, not open roles.
        u_path = urlparse(url).path.rstrip("/")
        b_path = urlparse(base_url).path.rstrip("/")
        if not u_path or u_path == b_path:
            continue
        slug = _slug_from_href(url)
        if not slug:
            continue
        if slug in out:
            continue
        title = text or slug
        out[slug] = JobPost(
            external_id=slug,
            title=title,
            url=url,
            posted_at=None,
        )
    return list(out.values())


@register
class CareersPageSource(SourceAdapter):
    """Fallback source for accounts with no ATS token (see module docstring)."""

    key = "ats_careers_page"
    tier = "http"
    cadence_hours = 168
    requires = ()  # no ats_token required — gated OFF at wiring when ATS matches

    def plan(self, account: Account, cursor: Optional[str]) -> list[FetchTask]:
        url = account.careers_url or f"https://{account.domain}/careers"
        return [
            FetchTask(
                source=self.key,
                url=url,
                domain=account.domain,
                meta={"vendor": "careers_page"},
            )
        ]

    def harvest_jobs(self, doc, account, task_meta):
        if not doc.body:
            return []
        html = doc.body.decode("utf-8", "replace")
        return parse_careers_page(html, doc.url or task_meta.get("url") or "")

    def parse(self, doc: Document, account: Account, task_meta: dict) -> list[SignalCandidate]:
        return []
