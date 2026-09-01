"""Pricing-page change detection from Wayback snapshots. PURE.

Conservative text-block diff of the pricing container: extract the set of
plan/price text blocks from each snapshot's pricing section and compare.
Any change in the block sets yields a single ``pricing_change`` candidate;
missing or unparseable pricing containers in EITHER html yield ``None`` —
never fabricate a signal from partial data.
"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Optional

from src.sources.base import SignalCandidate

CONFIDENCE = 0.65

# Attributes whose value mentions pricing — treat the element as the
# pricing container (or a nested pricing block).
_PRICING_ATTR_HINTS = ("pricing", "price", "plan")

# Text bits that identify a pricing line inside the container.
_PRICE_CHARS = set("0123456789$€£¥")


@dataclass(frozen=True)
class PricingBlock:
    """One extracted plan/price text block."""

    text: str


class _PricingParser(HTMLParser):
    """Collect text blocks (plan names / prices) inside a pricing container.

    The container is located by a class/id attribute containing a pricing
    hint; if no such attribute exists anywhere, the parser still collects
    standalone price-looking lines so pages whose pricing is un-marked HTML
    remain diffable. ``container_found`` records whether an explicit
    pricing container was seen — callers treat its absence as unparseable.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.container_found = False
        self._capture_depth = 0
        self._chunks: list[str] = []
        self.blocks: list[PricingBlock] = []

    # -- container / capture bookkeeping -----------------------------------

    def _attr_hit(self, attrs: list[tuple[str, Optional[str]]]) -> bool:
        for name in ("class", "id", "data-testid", "data-section"):
            for key, val in attrs:
                if key == name and val:
                    low = val.lower()
                    if any(hint in low for hint in _PRICING_ATTR_HINTS):
                        return True
        return False

    def handle_starttag(self, tag, attrs):
        if self._capture_depth == 0:
            if self._attr_hit(attrs):
                self.container_found = True
                self._capture_depth = 1
                self._chunks = []
                return
            # Standalone price line outside any pricing container (e.g.
            # a hero "$29/mo"). Only meaningful when the page marks its
            # pricing explicitly; we still capture so un-marked pages work.
            if tag in ("td", "li"):
                self._capture_depth = 1
                self._chunks = []
                return
        else:
            self._capture_depth += 1

    def handle_endtag(self, tag):
        if self._capture_depth > 0:
            self._capture_depth -= 1
            if self._capture_depth == 0:
                text = _clean_text(" ".join(self._chunks))
                if text and _looks_like_pricing_line(text):
                    self.blocks.append(PricingBlock(text))
                self._chunks = []

    def handle_data(self, data):
        if self._capture_depth > 0 and data.strip():
            self._chunks.append(data.strip())


def _clean_text(raw: str) -> str:
    return " ".join(raw.split()).strip().lower()


def _looks_like_pricing_line(text: str) -> bool:
    """A block counts as plan/price text only if it carries price-ish data."""
    if not text:
        return False
    if not any(ch in _PRICE_CHARS for ch in text):
        return False
    # Reject pure noise: strings that are only punctuation/whitespace.
    if not any(ch.isalnum() for ch in text):
        return False
    return True


def extract_pricing_blocks(html: str) -> list[PricingBlock] | None:
    """Extract plan/price text blocks.

    Returns None when the html has no parseable pricing container — the
    caller must NOT fabricate a diff from that.
    """
    if not html or not isinstance(html, (str, bytes)):
        return None
    if isinstance(html, bytes):
        try:
            html = html.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return None
    parser = _PricingParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return None
    if not parser.container_found:
        return None
    return parser.blocks


def diff_pricing(prev_html, curr_html, *, domain: str, today: str) -> Optional[SignalCandidate]:
    """Diff the pricing containers of two pricing-page snapshots.

    Conservative: missing/unparseable container in either html -> None.
    Any change in the extracted block sets -> one ``pricing_change``
    candidate. Identical sets -> None.
    """
    prev_blocks = extract_pricing_blocks(prev_html)
    curr_blocks = extract_pricing_blocks(curr_html)
    if prev_blocks is None or curr_blocks is None:
        return None

    prev_set = {b.text for b in prev_blocks}
    curr_set = {b.text for b in curr_blocks}
    if prev_set == curr_set:
        return None

    added = sorted(curr_set - prev_set)
    removed = sorted(prev_set - curr_set)
    summary_parts = []
    if added:
        summary_parts.append("added: " + "; ".join(added))
    if removed:
        summary_parts.append("removed: " + "; ".join(removed))
    title = "Pricing page changed"
    return SignalCandidate(
        signal_type="pricing_change",
        observed_at=today,
        natural_key=f"pricediff:{domain}:{today}",
        title=title,
        summary=" | ".join(summary_parts) or "pricing blocks changed",
        confidence=CONFIDENCE,
        evidence_data={
            "added": added,
            "removed": removed,
            "prev_blocks": sorted(prev_set),
            "curr_blocks": sorted(curr_set),
            "basis": "wayback_pricing_text_diff",
        },
    )
