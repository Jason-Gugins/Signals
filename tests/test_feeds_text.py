"""Tests for the pure html_to_text helper in src/sources/news/feeds.py."""

from __future__ import annotations

from src.sources.news.feeds import html_to_text


def test_nested_tags_yield_plain_sentence():
    body = "<div><p>We are <b>consolidating</b> data across teams.</p></div>"
    out = html_to_text(body)
    assert out == "We are consolidating data across teams."
    assert "<" not in out and ">" not in out


def test_script_and_style_contents_dropped():
    body = (
        "<p>Visible copy.</p>"
        "<script>var consolidat = 'consolidating data everywhere';</script>"
        "<style>.x { color: red; }</style>"
        "<p>More copy.</p>"
    )
    out = html_to_text(body)
    assert "Visible copy." in out
    assert "More copy." in out
    assert "consolidat" not in out
    assert "color: red" not in out


def test_plain_text_whitespace_collapse():
    assert html_to_text("hello   world") == "hello world"


def test_empty_and_none_return_empty_string():
    assert html_to_text("") == ""
    assert html_to_text(None) == ""


def test_large_input_does_not_raise():
    big = "<p>" + ("x" * 100_000) + "</p>"
    out = html_to_text(big)
    assert isinstance(out, str)
    assert out.startswith("x")


def test_markdownish_lt_and_unterminated_tag_do_not_crash():
    md = "Wrap the operator in an inline code span: `a < b` and move on."
    out_md = html_to_text(md)
    assert isinstance(out_md, str)
    assert "a < b" in out_md

    out_unterminated = html_to_text("Intro text <div")
    assert isinstance(out_unterminated, str)
    assert "<div" in out_unterminated


def test_entity_decoding():
    assert html_to_text("Tom &amp; Jerry") == "Tom & Jerry"
    # &nbsp; must not raise; it collapses away under whitespace normalisation.
    assert isinstance(html_to_text("a&nbsp;b"), str)
