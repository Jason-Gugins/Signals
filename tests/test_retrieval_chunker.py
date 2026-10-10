"""Chunker: spans are exact, boundaries never mid-word, overlap present."""
from __future__ import annotations

from src.retrieval.chunker import Chunk, chunk_text


def test_short_text_single_chunk_exact_span():
    text = "Acme is hiring a head of sales."
    chunks = chunk_text(text)
    assert len(chunks) == 1
    assert chunks[0].char_start == 0
    assert chunks[0].char_end == len(text)
    assert chunks[0].text == text
    assert text[chunks[0].char_start:chunks[0].char_end] == chunks[0].text


def test_spans_round_trip_on_long_text():
    text = ("Paragraph about security investigation and response. " * 40) + "\n\n" + (
        "Second paragraph about platform integration with workday. " * 40
    )
    chunks = chunk_text(text, target_tokens=100, overlap_tokens=20)
    assert len(chunks) > 1
    for c in chunks:
        assert text[c.char_start:c.char_end] == c.text
        assert c.token_est == max(1, len(c.text) // 4)


def test_chunks_never_split_midword_and_overlap():
    text = " ".join(f"word{i}" for i in range(2000))
    chunks = chunk_text(text, target_tokens=80, overlap_tokens=16)
    assert len(chunks) > 2
    for a, b in zip(chunks, chunks[1:]):
        # overlap: the next chunk starts inside the previous one's span
        assert b.char_start < a.char_end
        # never mid-word: every chunk boundary sits on whitespace in the source
        assert text[b.char_start - 1].isspace()
        assert not text[b.char_start].isspace() or b.char_start == 0
    # ordinals are dense from 0
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def test_empty_and_tiny_text():
    assert chunk_text("") == []
    assert chunk_text("   \n  ") == []
    assert len(chunk_text("ok")) == 1
