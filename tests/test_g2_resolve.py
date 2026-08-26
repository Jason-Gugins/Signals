from src.identity.g2_resolve import resolve_g2_slug, parse_g2_search_results


SEARCH_HTML = """
<html><body>
<div class="product-listing">
  <a href="/products/slack/reviews" class="product-listing__link">
    <div class="product-card__product-name">Slack</div>
  </a>
  <a href="/products/slack-technologies/reviews" class="product-listing__link">
    <div class="product-card__product-name">Slack Technologies</div>
  </a>
</div>
</body></html>
"""


def test_parse_g2_search_results():
    results = parse_g2_search_results(SEARCH_HTML)
    assert len(results) == 2
    assert results[0]["slug"] == "slack"
    assert results[0]["name"] == "Slack"

def test_resolve_g2_slug_picks_exact_match():
    results = parse_g2_search_results(SEARCH_HTML)
    slug = resolve_g2_slug("Slack", results)
    assert slug == "slack"

def test_resolve_g2_slug_picks_best_fuzzy_match():
    results = parse_g2_search_results(SEARCH_HTML)
    slug = resolve_g2_slug("Slack Technologies", results)
    assert slug == "slack-technologies"

def test_resolve_g2_slug_returns_none_on_empty():
    slug = resolve_g2_slug("Unknown", [])
    assert slug is None

def test_resolve_g2_slug_from_search_html():
    """End-to-end: HTML -> parse -> resolve."""
    slug = resolve_g2_slug("Slack", parse_g2_search_results(SEARCH_HTML))
    assert slug == "slack"
