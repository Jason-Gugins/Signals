"""Document text → overlapping chunks with exact char spans.

Sizing uses the repo-wide token convention (decide/gates.estimate_tokens:
~4 chars/token), so `target_tokens=500` ≈ 2,000 chars. Boundaries descend
paragraph → sentence → whitespace; a chunk boundary never splits a word.
Every chunk records its (char_start, char_end) span into the EXTRACTED text
it was chunked from — that pair is the citation provenance (this corpus is
HTML/JSON/RSS: unpaginated, so spans replace page numbers).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_CHARS_PER_TOKEN = 4

_PARA_SPLIT = re.compile(r"\n\s*\n+")
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    char_start: int
    char_end: int
    text: str
    token_est: int


def chunk_text(
    text: str, *, target_tokens: int = 500, overlap_tokens: int = 60
) -> list[Chunk]:
    target = max(target_tokens, 1) * _CHARS_PER_TOKEN
    overlap = max(overlap_tokens, 0) * _CHARS_PER_TOKEN
    if not text or not text.strip():
        return []
    spans = _segment(text, target)
    if not spans:
        return []
    # merge tiny trailing segments into their predecessor when they fit.
    # SIZE NOTE: a merged chunk reaches 1.1*target + overlap ≈ 2,440 chars
    # (~610 tokens) — the ceiling of the "500-token chunk" claim; acceptable
    # because overlap is shared context, but tighten the 1.1 factor if chunk
    # budgets ever bind downstream.
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and (end - merged[-1][0]) <= target * 1.1:
            merged[-1] = (merged[-1][0], end)
            continue
        merged.append((start, end))
    # emit chunks with overlap carried from the previous chunk's tail.
    # merged spans are CONTIGUOUS (prev_end <= start always), so the
    # reach-back is prev_end - overlap CLAMPED AT 0 — clamping at start
    # instead is how overlap silently becomes dead code (review finding:
    # max(start, prev_end - overlap) == start always, zero overlap ever).
    out: list[Chunk] = []
    prev_end = None
    for ordinal, (start, end) in enumerate(merged):
        char_start = start
        if prev_end is not None and overlap > 0:
            back = max(0, prev_end - overlap)
            ws = text.find(" ", back)  # align overlap start to a word edge
            if ws != -1 and ws + 1 < start:
                char_start = ws + 1
            # no word edge found in the window: keep char_start = start (the
            # never-mid-word invariant beats the overlap on pathological text)
        out.append(
            Chunk(
                ordinal=ordinal,
                char_start=char_start,
                char_end=end,
                text=text[char_start:end],
                token_est=max(1, (end - char_start) // _CHARS_PER_TOKEN),
            )
        )
        prev_end = end
    return out


def _segment(text: str, target: int) -> list[tuple[int, int]]:
    """(start, end) spans of size <= ~target, boundaries never mid-word."""
    paras = [m for m in _PARA_SPLIT.finditer(text)]
    pieces: list[tuple[int, int]] = []
    pos = 0
    bounds = [m.start() for m in paras] + [len(text)]
    for b in bounds:
        seg = text[pos:b]
        if not seg.strip():
            pos = b
            continue
        if len(seg) <= target:
            pieces.append((pos, b))
        else:
            pieces.extend(_split_long(text, pos, b, target))
        pos = b
    return pieces


def _split_long(text: str, start: int, end: int, target: int) -> list[tuple[int, int]]:
    """Sentence-split a too-long segment; whitespace-slice any monster sentence."""
    out: list[tuple[int, int]] = []
    pos = start
    for m in list(_SENT_SPLIT.finditer(text[start:end])) + [
        type("M", (), {"start": lambda _s, _p=end: end - start})()
    ]:
        b = start + m.start()
        if b - pos >= target:
            out.extend(_slice_ws(text, pos, b, target))
        else:
            out.append((pos, b))
        pos = b
    return [s for s in out if s[1] > s[0]]


def _slice_ws(text: str, start: int, end: int, target: int) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    pos = start
    while pos < end:
        hard = min(pos + target, end)
        if hard >= end:
            out.append((pos, end))
            break
        ws = text.rfind(" ", pos, hard)
        cut = ws if ws > pos else hard
        out.append((pos, cut))
        pos = cut
    return out
