"""Product Hunt launch-page parser. PURE. STUB — fixture-only per P2 spike.

LIVE FETCHING IS CF-BLOCKED: the P2 source spike
(``data/probe/P2_SOURCE_SPIKE.md``, 2026-08-31) probed
``https://www.producthunt.com/products/notion`` once and got a **403 with a
Cloudflare managed challenge on the FIRST request** ("Just a moment...",
challenges.cloudflare.com in the CSP). The abort rule was applied — the host
was not touched again. There is no live extraction candidate; this module and
its adapter (``ProductHuntSource``, key ``content_producthunt``) are a STUB
pending a browser-tier capture. Do not point the http fetcher at
producthunt.com.

The parser below is written ONLY against the synthetic fixture
``tests/fixtures/content/producthunt_launches.html`` (PH ``data-test``
attribute markup written from PH's known conventions — product cards are
``div[data-test^="product-item"]`` elements and the title link is an
``<a href^="/products/">``). Selectors MUST be re-validated against a real
browser-tier capture before this source ever emits live candidates.
"""

from __future__ import annotations

import re
from datetime import date
from html.parser import HTMLParser
from urllib.parse import urlparse

from src.core.models import Account
from src.identity.domains import root_domain
from src.identity.names import normalize_name
from src.sources.base import FetchTask, SignalCandidate, SourceAdapter
from src.sources.registry import register

PH_BASE = "https://www.producthunt.com"

_ALTERNATIVE_RE = re.compile(r"\balternatives?\b|\bvs\.?\b", re.I)


def ph_slug(name: str | None) -> str:
    """Deterministic product-slug guess for plan(): lowercase, hyphenated."""
    return re.sub(r"[^a-z0-9]+", "-", (name or "").casefold()).strip("-")


def producthunt_url(slug: str) -> str:
    return f"{PH_BASE}/products/{slug}"


def _attr(attrs, name):
    for k, v in attrs:
        if k == name:
            return v
    return None


class _PHLaunchParser(HTMLParser):
    """Collect product cards delimited by ``data-test^="product-item"``."""

    def __init__(self):
        super().__init__()
        self.items: list[dict] = []
        self._cur: dict | None = None
        self._div_depth = 0
        self._item_open_depth = 0
        self._mode: str | None = None

    def handle_starttag(self, tag, attrs):
        dt = _attr(attrs, "data-test") or ""
        if tag == "div":
            if self._cur is None and dt.startswith("product-item"):
                self._cur = {}
                self._item_open_depth = self._div_depth
                self._mode = None
            elif dt == "product-item-tagline":
                self._mode = "tagline"
            elif dt == "product-item-votes":
                self._mode = "votes"
            self._div_depth += 1
            return
        if self._cur is None:
            return
        if tag == "a":
            href = _attr(attrs, "href") or ""
            if href.startswith("/products/"):
                if not self._cur.get("slug"):
                    self._cur["slug"] = href.rstrip("/").rsplit("/", 1)[-1] or None
                    self._mode = "name"
                else:
                    self._mode = None
            elif dt == "link-product-website":
                self._cur["website"] = href or None
                self._mode = None
            return
        if dt == "product-item-tagline":
            self._mode = "tagline"
        elif dt == "product-item-votes":
            self._mode = "votes"
        elif dt and dt.startswith("product-item"):
            self._mode = None

    def handle_data(self, data):
        if self._cur is None or not self._mode:
            return
        if self._mode == "votes":
            m = re.search(r"\d+", data)
            if m:
                self._cur["votes"] = int(m.group(0))
        else:
            self._cur[self._mode] = (self._cur.get(self._mode) or "") + data

    def handle_endtag(self, tag):
        if tag != "div":
            return
        self._div_depth -= 1
        if self._cur is not None and self._div_depth == self._item_open_depth:
            if self._cur.get("slug") or self._cur.get("name"):
                for k in ("name", "tagline"):
                    if self._cur.get(k):
                        self._cur[k] = " ".join(self._cur[k].split())
            self.items.append(self._cur)
            self._cur = None
            self._mode = None

    def close(self):
        super().close()
        if self._cur is not None and (self._cur.get("slug") or self._cur.get("name")):
            self.items.append(self._cur)
        self._cur = None


def parse_producthunt_launches(html: str) -> list[dict]:
    """Parse a Product Hunt products/launch page into item dicts. Pure.

    Built ONLY against the synthetic fixture (see module docstring): real PH
    markup was never captured — the spike hit a Cloudflare managed challenge
    on the first request. Each item: {slug, name, tagline, website, votes}.
    """
    if not html:
        return []
    p = _PHLaunchParser()
    p.feed(html)
    p.close()
    return p.items


def launches_to_candidates(items, account: Account, *, today: date) -> list[SignalCandidate]:
    """Map parsed PH items to candidates. Pure.

    - own-domain / own-product-name matches -> ``product_launch``
      (natural key ``ph:<slug>``)
    - alternatives-list mentions of the account's name in a tagline ->
      ``intent_3rd_topic`` (natural key ``phalt:<slug>``)
    """
    want = normalize_name(account.name) if account.name else None
    want_domain = root_domain(account.domain) if account.domain else None
    out: list[SignalCandidate] = []
    for it in items:
        slug = it.get("slug")
        if not slug:
            continue
        name = it.get("name") or ""
        url = producthunt_url(slug)
        website = it.get("website") or ""
        host = urlparse(website).hostname or ""
        website_root = root_domain(host) if host else None
        own = bool(
            (want and want in (normalize_name(name) or ""))
            or (want_domain and website_root and website_root == want_domain)
        )
        if own:
            out.append(
                SignalCandidate(
                    signal_type="product_launch",
                    observed_at=today.isoformat(),
                    natural_key=f"ph:{slug}",
                    title=name,
                    url=url,
                    confidence=0.7,
                    evidence_data={"tagline": it.get("tagline"), "votes": it.get("votes"), "website": website or None},
                )
            )
            continue
        tagline = it.get("tagline") or ""
        if want and want in (normalize_name(tagline) or "") and _ALTERNATIVE_RE.search(tagline):
            out.append(
                SignalCandidate(
                    signal_type="intent_3rd_topic",
                    observed_at=today.isoformat(),
                    natural_key=f"phalt:{slug}",
                    title=name,
                    url=url,
                    confidence=0.5,
                    evidence_data={"tagline": tagline, "votes": it.get("votes")},
                )
            )
    return out


@register
class ProductHuntSource(SourceAdapter):
    """Product Hunt launches (key ``content_producthunt``). STUB.

    Live fetching is CF-blocked per the P2 spike — a Cloudflare managed
    challenge was served on the very first request to www.producthunt.com
    (see ``data/probe/P2_SOURCE_SPIKE.md``) — so the http tier cannot reach
    real markup and this adapter is a stub pending a browser-tier capture.
    plan() still builds the conventional ``/products/<slug>`` URL so the
    pipeline shape is ready; parse() is only ever exercised against the
    synthetic fixture in tests.
    """

    key = "content_producthunt"
    tier = "http"
    cadence_hours = 168

    def plan(self, account, cursor):
        slug = ph_slug(account.name or account.domain)
        return [FetchTask(source=self.key, url=producthunt_url(slug), domain=account.domain)]

    def parse(self, doc, account, task_meta):
        if not doc.body:
            return []
        items = parse_producthunt_launches(doc.body.decode("utf-8", "replace"))
        return launches_to_candidates(items, account, today=date.fromisoformat(task_meta["today"]))
