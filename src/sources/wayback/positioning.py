"""Homepage positioning change detection from Wayback snapshots. PURE.

Extracts the ``<title>`` and the meta description / og:description from two
homepage snapshots and emits ONE ``positioning_change`` candidate when the
normalized title or description changed. Values are normalized (whitespace
collapse + casefold) before comparison. Conservative: a missing/empty value
on either side never fabricates a diff — identical pages, an empty previous
snapshot, or unparseable html all yield ``None``. Never guess.
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Optional

from src.sources.base import SignalCandidate

CONFIDENCE = 0.6


class _PageMeta(HTMLParser):
    """Extract <title> text and the meta description / og:description.

    Local adaptation of the techstack ``_Page`` extractor pattern (stdlib
    ``html.parser`` — purity-safe). ``title``/``description`` stay ``None``
    when absent so the caller can distinguish "missing" from "empty".
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: Optional[str] = None
        self.description: Optional[str] = None
        self._og_description: Optional[str] = None
        self._in_title = False
        self._title_chunks: list[str] = []

    def _close_title(self) -> None:
        if self._in_title:
            self._in_title = False
            if self.title is None:
                self.title = " ".join("".join(self._title_chunks).split()) or None

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            # A real <title> cannot contain markup: only start a capture when
            # none is open (guards against malformed nested title tags).
            if not self._in_title:
                self._in_title = True
                self._title_chunks = []
            return
        # An unclosed <title> must not swallow the rest of the document:
        # any subsequent element closes the capture.
        self._close_title()
        if tag == "meta":
            ad = dict(attrs)
            key = (ad.get("name") or ad.get("property") or "").strip().lower()
            content = ad.get("content") or ""
            if key == "description" and self.description is None:
                self.description = content
            elif key == "og:description" and self._og_description is None:
                self._og_description = content

    def handle_endtag(self, tag):
        if tag == "title":
            self._close_title()

    def handle_data(self, data):
        if self._in_title:
            self._title_chunks.append(data)

    def close(self):
        # Finalize a <title> left open at EOF.
        super().close()
        self._close_title()

    @property
    def effective_description(self) -> Optional[str]:
        """meta description preferred, og:description as the fallback."""
        if self.description is not None:
            return self.description
        return self._og_description


def extract_page_meta(html) -> Optional[tuple[Optional[str], Optional[str]]]:
    """Extract (title, description) from an html document.

    Accepts str or bytes. Returns ``None`` when the html is empty or
    unparseable — the caller must NOT fabricate a diff from that.
    """
    if not html or not isinstance(html, (str, bytes)):
        return None
    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="replace")
    parser = _PageMeta()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return None
    return parser.title, parser.effective_description


def _collapse(value: Optional[str]) -> str:
    return " ".join(value.split()) if value else ""


def _norm(value: Optional[str]) -> str:
    return _collapse(value).casefold()


def diff_positioning(prev_html, curr_html, *, domain: str, today: str) -> Optional[SignalCandidate]:
    """Diff the title/meta-description of two homepage snapshots.

    Conservative: missing/unparseable html, an empty previous snapshot, or a
    changed value that is empty on either side -> ``None``. A normalized
    title OR description change (both sides non-empty) -> one
    ``positioning_change`` candidate keyed ``poschg:{domain}:{today}``.
    """
    prev = extract_page_meta(prev_html)
    curr = extract_page_meta(curr_html)
    if prev is None or curr is None:
        return None
    prev_title, prev_desc = prev
    curr_title, curr_desc = curr

    # A change only counts when BOTH sides carry a non-empty value: a value
    # that appears (or disappears) between snapshots is missing data, not
    # repositioning.
    prev_title_n, curr_title_n = _norm(prev_title), _norm(curr_title)
    prev_desc_n, curr_desc_n = _norm(prev_desc), _norm(curr_desc)
    title_changed = bool(prev_title_n) and bool(curr_title_n) and prev_title_n != curr_title_n
    desc_changed = bool(prev_desc_n) and bool(curr_desc_n) and prev_desc_n != curr_desc_n
    if not (title_changed or desc_changed):
        return None

    new_title = _collapse(curr_title)
    candidate_title = new_title or _collapse(curr_desc)[:60]
    return SignalCandidate(
        signal_type="positioning_change",
        observed_at=today,
        natural_key=f"poschg:{domain}:{today}",
        title=candidate_title,
        summary="Homepage title/meta-description changed (wayback snapshot diff)",
        confidence=CONFIDENCE,
        evidence_data={
            "old_title": _collapse(prev_title),
            "new_title": new_title,
            "old_description": _collapse(prev_desc),
            "new_description": _collapse(curr_desc),
            "basis": "wayback_homepage_meta_diff",
        },
    )
