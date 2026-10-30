"""sitecontent — sitemap-enumerated first-party prose pages (spike arm C).

Sourcing-spike verdicts that gate this adapter
(``data/probe/SOURCING_SPIKE_20261030.md``):

- Arm C GO: rillet case-study/compare pages are server-rendered 200s with
  real prose (84-93KB stripped per page); robots does not disallow the
  patterns.
- Arm B NO-GO: no RSS feed exists (all feed paths 404), so ``/blog/`` URLs
  fold into this adapter's pattern list.

Design: the account's stored sitemap document (e.g. rillet's
``ats_discovery`` doc ``037a2f69…`` — 376 ``<loc>`` URLs, body-sniffed
kind=sitemap despite its rss+xml content type) is the URL catalog. ``plan()``
extracts ``<loc>`` URLs (stdlib regex), keeps only same-site URLs matching
the configured content patterns, skips URLs the documents table already
holds, and emits at most ``max_fetches_per_cycle`` fetches per cycle,
advancing a rotation cursor (the last planned URL, stored in
``source_cursors`` via ``next_cursor``) so successive cycles cover the rest.
Fetched page bodies land in the raw store through the runner's standard
flow; the implement/claim pipeline reads them by recency (``iter_docs``),
which is the point of the source: the extraction validation proved the
corpus lacks need-bearing prose because these pages were LISTED but never
FETCHED.

Robots posture: all fetches go through the standard http fetcher tier —
``robots.txt`` is honored by default (``http.respect_robots``); the spike
verified rillet's robots allows the patterned paths. Public marketing /
case-study pages are public business content; the README's legal paragraph
applies unchanged.

Purity note: ``plan()`` does no network I/O and reads no clock — its only
reads are the LOCAL document store (the same reads the ``needs`` collector
makes through its injected db). The runner has no injection seam for
``plan`` (``needs`` gets its db via ``local_harvest``, workday via a
``detail_selection`` task_meta injection), so the store handles are resolved
through ``_shared_context()`` — the same Database path the pipeline uses —
and are injectable per call (``plan(account, cursor, db=..., store=...)``)
for tests.

Signal posture: the candidate is a thin ``content_appearance`` marker (the
taxonomy type the sibling content sources — itunes/blog/linkedin posts —
already use for first-party content), emitted ONLY when the page renders at
least ``prose_floor_chars`` of stripped prose. A shell page is stored but
stays signal-silent: the value is the stored text, never a synthetic event.
"""

from __future__ import annotations

import re
from html import unescape
from urllib.parse import urlparse

from loguru import logger

from src.core.doccontent import sniff
from src.core.models import Account, Document
from src.core.textutil import html_to_text, stable_id, truncate
from src.sources.base import FetchTask, SignalCandidate, SourceAdapter
from src.sources.registry import register
from src.sources.sitecontent.config import (
    DEFAULT_CFG,
    DEFAULT_MAX_PROSE_CHARS,
    load_sitecontent_cfg,
)

__all__ = [
    "DEFAULT_CFG",
    "SiteContentSource",
    "extract_locs",
    "filter_patterns",
    "page_prose",
    "same_site",
]

#: <loc> extraction: stdlib regex, XML-escaped ampersands unescaped.
_LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.I | re.S)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)

#: Body-sniff attempts per plan() call (newest docs first): bounding the
#: gzip reads keeps a sitemap-less domain's weekly cycle cheap.
_SNIFF_LIMIT = 25


def _decode(body) -> str:
    """Body bytes -> text with a replacement policy; never raises."""
    if body is None:
        return ""
    if isinstance(body, (bytes, bytearray)):
        return bytes(body).decode("utf-8", errors="replace")
    return str(body)


def extract_locs(sitemap_text: str) -> list[str]:
    """Absolute http(s) ``<loc>`` URLs from a sitemap XML, deduped in order. PURE."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in _LOC_RE.findall(sitemap_text or ""):
        loc = unescape((raw or "").strip())
        if not loc.startswith(("http://", "https://")) or loc in seen:
            continue
        seen.add(loc)
        out.append(loc)
    return out


def filter_patterns(urls: list[str], patterns: list[str]) -> list[str]:
    """URLs containing any configured pattern (casefold compare). PURE."""
    pats = [str(p).casefold() for p in (patterns or []) if p]
    if not pats:
        return []
    return [u for u in urls if any(p in u.casefold() for p in pats)]


def same_site(url: str, domain: str) -> bool:
    """True when the URL's host is the account's domain or a subdomain of it.

    A sitemap must never aim the fetcher at other hosts (a hijacked or
    third-party sitemap would otherwise turn this source into an open
    crawler). ``www.`` passes naturally (``www.rillet.com`` ends with
    ``.rillet.com``).
    """
    host = (urlparse(url or "").hostname or "").casefold()
    root = (domain or "").casefold().strip()
    if not host or not root:
        return False
    return host == root or host.endswith("." + root)


def page_prose(body, *, max_chars: int = DEFAULT_MAX_PROSE_CHARS) -> dict:
    """Title + stripped prose from a content page, capped at ``max_chars``. PURE.

    ``html_to_text`` is the same renderer the needs/retrieval layers apply to
    stored HTML, so the prose measured here is the prose the claim pipeline
    will see. Never raises; bytes or str accepted.
    """
    html = _decode(body)
    if not html:
        return {"title": None, "text": ""}
    m = _TITLE_RE.search(html)
    title = html_to_text(m.group(1)) if m else None
    text = html_to_text(html)
    if max_chars and len(text) > max_chars:
        text = text[:max_chars]
    return {"title": title or None, "text": text}


# --------------------------------------------------------------------------
# store resolution: the runner has no plan()-injection seam, so the default
# context opens the pipeline's own Database/RawStore paths once per process.
# Tests pass db=/store= explicitly; both entry points are injectable.
# --------------------------------------------------------------------------

_SHARED = None


def _shared_context():
    """(Database, RawStore) on the pipeline's configured paths, cached."""
    global _SHARED
    if _SHARED is None:
        try:
            from src.core.config import Config
            from src.core.db import Database
            from src.core.rawstore import RawStore

            cfg = Config.load()
            db = Database(cfg.storage.db_path)
            _SHARED = (db, RawStore(db, cfg.storage.raw_dir))
        except Exception as exc:  # pragma: no cover — misconfigured host
            logger.warning("sitecontent: store unavailable, planning nothing: {}", exc)
            return None
    return _SHARED


def _select_sitemap_text(db, store, domain: str) -> str | None:
    """Newest stored document for the domain whose BODY sniffs as a sitemap.

    Documents whose URL mentions ``sitemap`` are tried first (the common
    case); the BODY sniff — not the content type — decides, because the
    measured rillet sitemap ships as ``application/rss+xml``. Returns the
    decoded XML text, or None when the account has no stored sitemap
    (the source plans nothing rather than guessing a URL).
    """
    rows = db.query(
        "SELECT doc_id, url, content_type FROM documents WHERE domain=? "
        "ORDER BY fetched_at DESC",
        (domain,),
    )
    rows = sorted(
        rows,
        key=lambda r: 0 if "sitemap" in str(r.get("url") or "").casefold() else 1,
    )
    for row in rows[:_SNIFF_LIMIT]:
        try:
            doc = store.get(row.get("doc_id"))
        except Exception:
            continue  # unreadable body: try the next candidate, never raise
        if doc is None:
            continue
        text = _decode(getattr(doc, "body", None))
        if not text.strip():
            continue
        if sniff(str(row.get("content_type") or ""), text) == "sitemap":
            return text
    return None


@register
class SiteContentSource(SourceAdapter):
    key = "sitecontent"
    tier = "http"
    cadence_hours = 168  # weekly: content pages change slowly; 20/cycle covers
    requires: tuple[str, ...] = ()
    emits = ("content_appearance",)

    def plan(self, account: Account, cursor, *, db=None, store=None) -> list[FetchTask]:
        """Pattern-filtered, rotation-capped fetch list from the stored sitemap.

        Requires a stored sitemap-kind document for the domain (body-sniffed
        via :func:`src.core.doccontent.sniff`); without one, plans nothing.
        Deterministic given (account, cursor, local store): locs are sorted,
        the cursor is the previous cycle's last planned URL, and already-
        stored URLs are skipped so a cycle never re-fetches a page the
        documents table holds.
        """
        if db is None or store is None:
            shared = _shared_context()
            if shared is None:
                return []
            # Fill ONLY the missing handle: a caller that owns one handle
            # (tests, tooling) must never have it silently mixed with the
            # shared live store.
            if db is None:
                db = shared[0]
            if store is None:
                store = shared[1]
        cfg = load_sitecontent_cfg()
        text = _select_sitemap_text(db, store, account.domain)
        if not text:
            return []
        locs = sorted(
            set(filter_patterns([u for u in extract_locs(text) if same_site(u, account.domain)], cfg["content_patterns"]))
        )
        if not locs:
            return []
        fetched = {
            str(r.get("url") or "").rstrip("/")
            for r in db.query(
                "SELECT url FROM documents WHERE domain=?", (account.domain,)
            )
        }
        # Rotation: resume AFTER the last planned URL (wrap around); an
        # unknown/stale cursor (the sitemap changed) restarts from the head.
        start = 0
        marker = str(cursor or "").strip()
        if marker:
            if marker in locs:
                start = (locs.index(marker) + 1) % len(locs)
        cap = max(0, int(cfg["max_fetches_per_cycle"]))
        out: list[FetchTask] = []
        for i in range(len(locs)):
            url = locs[(start + i) % len(locs)]
            if url.rstrip("/") in fetched:
                continue
            out.append(
                FetchTask(
                    source=self.key,
                    url=url,
                    domain=account.domain,
                    meta={"kind": "page"},
                )
            )
            if len(out) >= cap:
                break
        return out

    def next_cursor(self, doc: Document, candidates: list[SignalCandidate]):
        """The rotation cursor is the last fetched page's URL (stored in
        ``source_cursors.cursor`` by the runner): the next plan() resumes
        after it, so the per-cycle cap never starves the tail of the list."""
        return getattr(doc, "url", None) or None

    def parse(self, doc: Document, account: Account, task_meta: dict) -> list[SignalCandidate]:
        """Thin ``content_appearance`` marker above the prose floor; else [].

        The body is already stored by the runner's raw-store flow — this
        method only decides whether the page earned its marker: at least
        ``prose_floor_chars`` of stripped prose (shells, redirects and
        challenge pages stay signal-silent). The observed date comes from the
        runner-injected ``today`` (or the document's fetch date); with
        neither, nothing is emitted — never a guessed date.
        """
        if not getattr(doc, "body", None):
            return []
        cfg = load_sitecontent_cfg()
        prose = page_prose(doc.body, max_chars=cfg["max_prose_chars"])
        text = prose["text"]
        if len(text.strip()) < cfg["prose_floor_chars"]:
            return []
        meta = task_meta or {}
        observed = str(meta.get("today") or (getattr(doc, "fetched_at", None) or ""))[:10]
        if not observed:
            return []
        url = getattr(doc, "url", None) or ""
        return [
            SignalCandidate(
                signal_type="content_appearance",
                observed_at=observed,
                natural_key=f"sitecontent:{account.domain}:{stable_id(url, length=12)}",
                title=(prose["title"] or None) and str(prose["title"])[:160],
                summary=truncate(text, 280),
                url=url or None,
                confidence=0.6,
                domain_override=account.domain,
                evidence_data={
                    "doc_id": getattr(doc, "doc_id", None),
                    "chars": len(text),
                    "kind": "page",
                },
            )
        ]
