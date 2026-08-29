"""Runner routing for marketplace_g2: prefer the rendered reviews_and_filters fragment.

G2 serves reviews as client-rendered elv-* DOM in the
``/products/{slug}/reviews_and_filters`` HTML fragment; the plain
``/products/{slug}/reviews`` page is only the app shell with no review cards.
The runner must therefore fetch the fragment through the DataDome stealth
browser so ``parse()``/``harvest_reviews()`` receive the actual rendered reviews
in the Document body.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.core.http import FetchResult
from src.core.models import Document
from src.sources.base import FetchTask

RENDERED = b"<html><body><article id='sierra-review-123' class='elv-stars elv-stars-9'>...review...</article></body></html>"


def _make_runner(stub_fetcher, datadome_bypass, browser=None):
    from src.pipeline.runner import CollectorRunner

    config = SimpleNamespace(
        cloudflare=SimpleNamespace(enabled=True),
        datadome=SimpleNamespace(enabled=True),
        http=SimpleNamespace(user_agent="test-ua"),
        browser=SimpleNamespace(proxy_server=None),
    )
    runner = CollectorRunner.__new__(CollectorRunner)
    runner.config = config
    runner.db = None
    runner.fetcher = stub_fetcher
    runner.browser = browser
    runner.cloudflare_bypass = None
    runner.datadome_bypass = datadome_bypass
    runner.store = MagicMock()
    runner.ctx = None
    return runner


def test_runner_routes_marketplace_g2_to_fragment_via_stealth(tmp_path):
    """marketplace_g2 tasks fetch the rendered reviews_and_filters fragment via the stealth browser."""
    rendered_doc = Document(
        doc_id="g2:1", source="marketplace_g2",
        url="https://www.g2.com/products/sierra/reviews_and_filters",
        body=RENDERED,
    )
    stealth_result = FetchResult(ok=True, status=200, doc=rendered_doc,
                                 cached=False, error=None, elapsed_ms=100)

    stealth_browser = MagicMock()
    stealth_browser.fetch.return_value = stealth_result
    datadome_bypass = MagicMock()
    datadome_bypass.stealth = stealth_browser

    stub_fetcher = MagicMock()  # must NOT be hit on the rendered path

    runner = _make_runner(stub_fetcher, datadome_bypass)

    task = FetchTask(source="marketplace_g2",
                     url="https://www.g2.com/products/sierra/reviews",
                     domain="g2.com",
                     meta={"kind": "reviews", "product_slug": "sierra", "page": 1})

    result = runner._fetch_one(task, None, None)

    # The stealth browser was asked for the rendered fragment URL, not the shell.
    stealth_browser.fetch.assert_called_once()
    fetched_url = stealth_browser.fetch.call_args[0][0]
    assert "reviews_and_filters" in fetched_url
    # The rendered fragment HTML becomes the Document body.
    assert result is not None
    assert result.doc is not None
    assert result.doc.body == RENDERED
    # The plain over-HTTP fetcher was not used on the rendered path.
    stub_fetcher.get.assert_not_called()

    # DataDome clears in HEADED mode only — the fragment path must run the
    # stealth browser headed (config default is headless: true) with a
    # behavioral warm-up (homepage visit + scroll + dwell) before navigating.
    call_kwargs = stealth_browser.fetch.call_args[1]
    assert call_kwargs.get("warmup_url"), "G2 fragment path must warm up like a human first"
    assert "g2.com" in call_kwargs["warmup_url"]


def test_runner_marketplace_g2_falls_back_when_no_stealth_slug(tmp_path):
    """Without a product_slug (and with no usable browser) the runner falls back to the fetcher."""
    datadome_bypass = MagicMock()  # no .stealth attribute exposed
    stub_fetcher = MagicMock()
    stub_fetcher.get.return_value = FetchResult(
        ok=False, status=403, doc=None, cached=False, error="HTTP 403", elapsed_ms=1
    )

    runner = _make_runner(stub_fetcher, datadome_bypass)

    task = FetchTask(source="marketplace_g2",
                     url="https://www.g2.com/products/slack/reviews",
                     domain="g2.com",
                     meta={"kind": "reviews"})  # no product_slug

    result = runner._fetch_one(task, None, None)

    # Falls back to the existing fetcher path; no stealth fragment fetch.
    stub_fetcher.get.assert_called()
    datadome_bypass.stealth.fetch.assert_not_called()
    assert result is not None
