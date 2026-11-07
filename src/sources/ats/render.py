"""ATS board payload -> careers text documents (the careers harvest's
Phase 1 render step).

The board API payloads are ALREADY stored in the raw corpus (greenhouse
``content=true``, ashby ``descriptionHtml``/``descriptionPlain`` — fetched
2026-10-08/30 with 200s). This module renders them into prose documents the
decide pipeline can gate and measure — careers prose is the only need-dense
source measured (2.6-5.1% vs ~0% homepages) and the G5 material wall is
PROSE-BOUND (decide-lab addendum 9), so this is the wall's fix.
"""

from __future__ import annotations

import re
from html import unescape

from src.core.textutil import html_to_text

_TAG = re.compile(r"<[^>]+>")


def _html_to_text(html: str) -> str:
    """html_to_text + entity cleanup for board payloads (escaped twice in
    some greenhouse payloads: &lt;p&gt; wrappers)."""
    text = html_to_text(html)
    if "&lt;" in text or "&amp;" in text:
        text = unescape(text)
        text = _TAG.sub(" ", text)
    return " ".join(text.split()).strip()


def _job_text(title: str, loc: str | None, body: str) -> str:
    head = f"{title}" + (f" — {loc}" if loc else "")
    return " ".join((head + "\n" + body).split())


def render_greenhouse_board(payload: dict, *, domain: str) -> list[dict]:
    """boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true ->
    [{"title", "url", "text", "domain"}] (jobs[].content is the HTML
    description; empty content degrades to a title-only doc — honest)."""
    out: list[dict] = []
    for j in payload.get("jobs") or []:
        if not isinstance(j, dict):
            continue
        title = str(j.get("title") or "").strip()
        if not title:
            continue
        loc = ((j.get("location") or {}) if isinstance(j.get("location"), dict) else {}).get("name")
        url = str(j.get("absolute_url") or "")
        body = _html_to_text(str(j.get("content") or ""))
        out.append({"domain": domain, "title": title, "url": url,
                    "text": _job_text(title, loc, body)})
    return out


def render_ashby_board(payload: dict, *, domain: str) -> list[dict]:
    """api.ashbyhq.com/posting-api/job-board/{board} -> the same shape.
    descriptionPlain is preferred (ashby's own text render); descriptionHtml
    is the fallback. ``location`` is a dict {city, region, ...}."""
    out: list[dict] = []
    for j in payload.get("jobs") or payload.get("jobPostings") or []:
        if not isinstance(j, dict):
            continue
        title = str(j.get("title") or "").strip()
        if not title:
            continue
        loc_raw = j.get("locationName") or j.get("location")
        loc = (loc_raw.get("city") or loc_raw.get("region")
               if isinstance(loc_raw, dict) else loc_raw)
        url = str(j.get("jobUrl") or j.get("applyUrl") or "")
        plain = str(j.get("descriptionPlain") or "").strip()
        body = " ".join(plain.split()) if plain else _html_to_text(str(j.get("descriptionHtml") or ""))
        out.append({"domain": domain, "title": title, "url": url,
                    "text": _job_text(title, loc, body)})
    return out


def careers_url_candidates(domain: str) -> list[str]:
    """The discovery pattern list for a domain's careers hub (the
    careers_page fallback only tries /careers — brumate 404'd there; the
    Shopify pattern /pages/careers and /about/careers cover the rest)."""
    cands = [
        f"https://{domain}/careers",
        f"https://{domain}/pages/careers",
        f"https://www.{domain}/pages/careers",
        f"https://{domain}/about/careers",
        f"https://www.{domain}/about/careers",
        f"https://{domain}/jobs",
    ]
    return sorted(set(cands))
