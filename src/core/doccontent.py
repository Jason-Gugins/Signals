"""Format sniffing + envelope human-content extraction — pure, no I/O.

Task 1 of extraction-before-claims (plan 2026-10-29): the accepted-claims read
traced ~80% of accepted-but-unpromotable claims to four machine formats
(ATS list JSON, app-store JSON, sitemap XML, RSS). This module sits BEFORE the
decide layer's claim gates: it sniffs the stored document format from the BODY
(content type as tiebreaker only — the measured sitemap ships as rss+xml) and
reduces envelope documents to their human content, excluding catalog formats
and wrong-entity items up front, with honest counts.

Discipline: every function is pure — body strings in, result dicts out. No
I/O, no clock, stdlib only (json, xml.etree), never raises on document content
(any malformed input degrades to text passthrough, which is today's behavior).
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET

from src.core.textutil import html_to_text

__all__ = ["MAX_TEXT_CHARS", "JOB_WINDOW", "sniff", "human_text", "publisher_matches"]

# Bounded implementer input: any text this module returns is capped here.
MAX_TEXT_CHARS = 120_000
# json_jobs extraction window: most recent N jobs by updated_at.
JOB_WINDOW = 100

_KINDS = ("json_jobs", "json_results", "rss", "sitemap", "robots", "html", "text")


def _cap(text: str) -> str:
    return text[:MAX_TEXT_CHARS]


def _result(
    kind: str,
    text: str,
    *,
    publisher_items: int = 0,
    excluded: bool = False,
    reason: str | None = None,
    wrong_entity_dropped: int = 0,
    unjudged: int = 0,
) -> dict:
    return {
        "kind": kind,
        "text": _cap(text),
        "publisher_items": publisher_items,
        "excluded": excluded,
        "reason": reason,
        "wrong_entity_dropped": wrong_entity_dropped,
        "unjudged": unjudged,
    }


def _field(value) -> str:
    """Coerce an envelope field to clean text (greenhouse `location` is a
    {"name": ...} dict in the wild; strings everywhere else)."""
    if isinstance(value, dict):
        value = value.get("name") or value.get("label") or ""
    if value is None:
        return ""
    return str(value).strip()


def sniff(content_type: str, body: str) -> str:
    """Classify a stored document body.

    kind ∈ {"json_jobs", "json_results", "rss", "sitemap", "robots", "html",
    "text"} — BODY-sniff first (the measured sitemap that ships as rss+xml
    content type is the lesson: its body is <urlset> and must classify as
    sitemap), content_type as tiebreaker only.

    Detection, in order:
    - JSON-parseable object: {"jobs": [...]} ⇒ json_jobs; {"results": [...]}
      where some result carries artistName ⇒ json_results. A dict without
      those shapes (or non-dict / malformed JSON) falls through — safe
      direction, passthrough is today's behavior.
    - XML markers in the head: <urlset ⇒ sitemap; <rss or <feed ⇒ rss.
    - User-agent: + Disallow ⇒ robots.
    - <html or <!DOCTYPE ⇒ html.
    - Tiebreaker: a markerless body under an html content type promotes to
      html (cosmetic — html/text pass through identically). No other
      content-type promotion exists.
    - Default: text.
    """
    s = (body or "").lstrip("\ufeff \t\r\n")
    if s.startswith("{"):
        try:
            data = json.loads(s)
        except Exception:
            data = None
        if isinstance(data, dict):
            if isinstance(data.get("jobs"), list):
                return "json_jobs"
            results = data.get("results")
            if isinstance(results, list) and any(
                isinstance(item, dict) and "artistName" in item for item in results
            ):
                return "json_results"
    head = s[:4096].lower()
    if "<urlset" in head:
        return "sitemap"
    if "<rss" in head or "<feed" in head:
        return "rss"
    if "user-agent:" in head and "disallow" in head:
        return "robots"
    if "<html" in head or "<!doctype" in head:
        return "html"
    if "html" in (content_type or "").lower():
        return "html"
    return "text"


def publisher_matches(publisher: str | None, hint: str | None, aliases=()) -> bool:
    """Publisher-vs-account matching for the wrong-entity guard.

    Rule: casefold both sides; MATCH if either string contains the other as a
    substring, or if any alias in ``aliases`` substring-matches either side
    (same rule, applied alias-vs-publisher and alias-vs-hint). An empty or
    None side never matches — an unmatchable publisher is "unjudged", never
    "wrong".

    Known edges, kept deliberately (crude > clever; measured against the real
    envelopes):
    - False NEGATIVE: distinct surface forms with no alias bridge do not
      match — "Meta Platforms" vs "Facebook"; "Zibma Infotech" vs "Rillet"
      (the measured wrong-entity case; correctly no match ⇒ dropped).
    - False POSITIVE: short alias fragments substring-match — alias "let"
      matches publisher "Rillet". Keep aliases brand-real ("rillet ai"),
      never fragments.
    - Measured true positives: "Databricks Inc." vs "Databricks" ⇒ match
      (containment); alias "zibma" vs publisher "Zibma Infotech" ⇒ match.
    """
    p = (publisher or "").casefold().strip()
    h = (hint or "").casefold().strip()
    if not p or not h:
        return False
    if p in h or h in p:
        return True
    for alias in aliases or ():
        a = (alias or "").casefold().strip()
        if a and (a in p or p in a or a in h or h in a):
            return True
    return False


def _json_jobs(body: str, publisher_hint: str | None) -> dict:
    data = json.loads(body)
    jobs = [job for job in (data.get("jobs") or []) if isinstance(job, dict)]
    # Most recent first by updated_at (else first_published, else file order —
    # the stable sort keeps equal keys in file order). Then the newest-100
    # window is the extraction set; per-job publisher matching drops
    # wrong-entity jobs from within that window.
    jobs.sort(
        key=lambda job: _field(job.get("updated_at")) or _field(job.get("first_published")),
        reverse=True,
    )
    window = jobs[:JOB_WINDOW]
    lines: list[str] = []
    publisher_items = dropped = unjudged = 0
    for job in window:
        title = _field(job.get("title"))
        location = _field(job.get("location"))
        company = _field(job.get("company_name"))
        if company:
            publisher_items += 1
            if publisher_hint and not publisher_matches(company, publisher_hint, ()):
                dropped += 1
                continue
        else:
            unjudged += 1  # no company_name ⇒ can't judge ≠ wrong: kept
        parts = [part for part in (title, location, company) if part]
        if parts:
            lines.append(" — ".join(parts))
    return _result(
        "json_jobs",
        "\n".join(lines),
        publisher_items=publisher_items,
        wrong_entity_dropped=dropped,
        unjudged=unjudged,
    )


def _json_results(body: str, publisher_hint: str | None) -> dict:
    data = json.loads(body)
    results = [item for item in (data.get("results") or []) if isinstance(item, dict)]
    lines: list[str] = []
    publisher_items = dropped = unjudged = 0
    for item in results:
        artist = _field(item.get("artistName"))
        track = _field(item.get("trackName"))
        description = _field(item.get("description"))
        if artist:
            publisher_items += 1
            if publisher_hint and not publisher_matches(artist, publisher_hint, ()):
                dropped += 1
                continue
        else:
            unjudged += 1  # missing artistName ⇒ kept, counted unjudged
        if track and description:
            lines.append(f"{track}: {description}")
        elif track or description:
            lines.append(track or description)
    return _result(
        "json_results",
        "\n".join(lines),
        publisher_items=publisher_items,
        wrong_entity_dropped=dropped,
        unjudged=unjudged,
    )


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _rss(body: str) -> dict:
    # Parse from bytes: ET.fromstring raises on a str carrying an
    # `<?xml encoding=...?>` declaration, which real feeds ship.
    try:
        root = ET.fromstring(body.encode("utf-8", "replace"))
    except Exception:
        root = None
    lines: list[str] = []
    if root is not None:
        for element in root.iter():
            if _local(element.tag) not in ("item", "entry"):
                continue
            title = ""
            description = ""
            for child in element:
                name = _local(child.tag)
                if name == "title" and not title:
                    title = (child.text or "").strip()
                elif name in ("description", "summary", "encoded", "content") and not description:
                    description = child.text or ""
            if description:
                # Measured: google_news item descriptions are anchor soup
                # around news.google.com redirect URLs — strip to anchor text.
                description = html_to_text(description)
            if title and description:
                lines.append(f"{title}: {description}")
            elif title or description:
                lines.append(title or description)
    text = "\n".join(lines).strip()
    if not text:
        # Zero items parsed (Atom/odd/broken feeds): fall back to text
        # passthrough — never silently empty.
        text = body
    return _result("rss", text)


def human_text(content_type: str, body: str, *, publisher_hint: str | None) -> dict:
    """Reduce a stored document to its human content, with honest bookkeeping.

    Returns {kind, text, publisher_items, excluded, reason,
    wrong_entity_dropped, unjudged}:

    - json_jobs  ({"jobs": [{title, location?, company_name?, updated_at?,
                   first_published?}, …]}): one line per job
                   "{title} — {location} — {company_name}", most recent 100
                   by updated_at (else first_published, else file order);
                   PER-JOB publisher matching vs publisher_hint — jobs whose
                   company_name fails the match are dropped and counted
                   (wrong_entity_dropped). Per-ITEM matching replaces any
                   doc-level majority (undefined on ties, and the measured
                   databricks iTunes doc mixes four distinct artistNames).
                   publisher_items counts window jobs carrying company_name.
    - json_results ({"results": [{artistName?, description?, trackName?},
                   …]}): PER-RESULT publisher matching on artistName —
                   mismatched results' descriptions are dropped and counted
                   (the Zibma case); results with a MISSING artistName are
                   KEPT and counted unjudged (can't judge ≠ wrong); text =
                   surviving descriptions labeled by trackName
                   ("{trackName}: {description}").
    - rss:       item titles + descriptions, item-description HTML stripped
                   to anchor text via textutil.html_to_text. Zero items
                   parsed ⇒ text passthrough (Atom/odd feeds: never silently
                   empty).
    - sitemap:   {"kind": "sitemap", "text": "", "excluded": True,
                  "reason": "catalog_url_list"} — a URL list has no
                  claim-worthy content; excluded before the doc gate.
    - robots:    text as-is (small, honest policy file).
    - html/text: body UNCHANGED — no html_to_text at this seam (routing
                  markup-bearing html docs through html_to_text would alter
                  staged text for every html doc, beyond the envelope scope).

    Malformed content that sniffs to a json kind ⇒ text passthrough (never
    raise). publisher_hint=None disables publisher matching (nothing is
    "wrong" without a hint to be wrong against). Any returned text is capped
    at MAX_TEXT_CHARS (bounded implementer input).
    """
    body = body or ""
    kind = sniff(content_type, body)
    try:
        if kind == "json_jobs":
            return _json_jobs(body, publisher_hint)
        if kind == "json_results":
            return _json_results(body, publisher_hint)
    except Exception:
        # Malformed JSON that sniffed json ⇒ text passthrough, never raise.
        return _result("text", body)
    if kind == "rss":
        return _rss(body)
    if kind == "sitemap":
        return _result("sitemap", "", excluded=True, reason="catalog_url_list")
    if kind in ("robots", "html"):
        return _result(kind, body)
    return _result("text", body)
