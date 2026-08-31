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

import logging

from src.core.http import FetchResult
from src.core.models import Document
from src.sources.base import FetchTask

RENDERED = b"<html><body><article id='sierra-review-123' class='elv-stars elv-stars-9'>...review...</article></body></html>"
CHALLENGE = b"<html><head><script>var dd={'rt':'ja'}</script><script src='https://geo.captcha-delivery.com/captcha/init.js'></script></head><body></body></html>"


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


def test_runner_closes_stealth_browser_after_run(tmp_path):
    """The stealth (Patchright) browser must be closed exactly once when the
    run finishes — it is a full headed Chromium (~300-500MB) and leaks if
    never closed."""
    stealth_browser = MagicMock()
    datadome_bypass = MagicMock()
    datadome_bypass.stealth = stealth_browser
    stub_fetcher = MagicMock()

    runner = _make_runner(stub_fetcher, datadome_bypass)
    runner.db = MagicMock()
    runner.stealth_browser = stealth_browser

    adapter = MagicMock()
    adapter.key = "marketplace_g2"
    adapter.plan.return_value = []
    adapter.follow_tasks.return_value = []
    account = MagicMock()

    runner.run([adapter], [account], force=True, dry_run=True)

    stealth_browser.close.assert_called_once()


def test_g2_fragment_warmup_only_on_page_1(tmp_path):
    """The behavioral warm-up (homepage + dwell) is ~4s of extra DataDome
    exposure per fetch. It must run on page 1 (session establishment) but NOT
    on pagination pages 2..N (the session is already warm)."""
    page1 = Document(doc_id="p1", source="marketplace_g2",
                     url="https://www.g2.com/products/sierra/reviews_and_filters",
                     body=b"<html>page1</html>")
    stealth_browser = MagicMock()
    stealth_browser.fetch.return_value = FetchResult(
        ok=True, status=200, doc=page1, cached=False, error=None, elapsed_ms=10)
    datadome_bypass = MagicMock()
    datadome_bypass.stealth = stealth_browser
    runner = _make_runner(MagicMock(), datadome_bypass)

    task1 = FetchTask(source="marketplace_g2", url="https://www.g2.com/products/sierra/reviews",
                      domain="g2.com", meta={"product_slug": "sierra", "page": 1})
    runner._fetch_one(task1, None, None)
    kw1 = stealth_browser.fetch.call_args[1]
    assert kw1.get("warmup_url"), "page 1 must warm up"

    stealth_browser.fetch.reset_mock()
    task2 = FetchTask(source="marketplace_g2", url="https://www.g2.com/products/sierra/reviews?page=2",
                      domain="g2.com", meta={"product_slug": "sierra", "page": 2})
    runner._fetch_one(task2, None, None)
    kw2 = stealth_browser.fetch.call_args[1]
    assert not kw2.get("warmup_url"), "pages 2..N must NOT re-run the warm-up"


def test_follow_tasks_page2_refetches_fragment_page_2(tmp_path):
    """End-to-end two-pass: after page 1 parses, follow_tasks plans page=2 and
    _fetch_one fetches the *fragment* for page 2 through the stealth browser."""
    page1_html = b"<html><article id='sierra-review-1' ue='track-in-viewport'>x</article></html>"
    page2_html = b"<html><article id='sierra-review-2' ue='track-in-viewport'>y</article></html>"
    stealth_browser = MagicMock()
    stealth_browser.fetch.side_effect = [
        FetchResult(ok=True, status=200,
                    doc=Document(doc_id="f1", source="marketplace_g2",
                                 url="https://www.g2.com/products/sierra/reviews_and_filters?page=1",
                                 body=page1_html),
                    cached=False, error=None, elapsed_ms=10),
        FetchResult(ok=True, status=200,
                    doc=Document(doc_id="f2", source="marketplace_g2",
                                 url="https://www.g2.com/products/sierra/reviews_and_filters?page=2",
                                 body=page2_html),
                    cached=False, error=None, elapsed_ms=10),
    ]
    datadome_bypass = MagicMock()
    datadome_bypass.stealth = stealth_browser
    runner = _make_runner(MagicMock(), datadome_bypass)

    from src.sources.marketplace.collector import MarketplaceG2Source
    from src.core.models import Account
    adapter = MarketplaceG2Source()
    account = Account(domain="sierra.com", g2_slug="sierra")

    # pass 1
    task1 = FetchTask(source="marketplace_g2", url="https://www.g2.com/products/sierra/reviews",
                      domain="g2.com", meta={"kind": "reviews", "product_slug": "sierra", "page": 1})
    result1 = runner._fetch_one(task1, None, None)
    meta1 = {"kind": "reviews", "product_slug": "sierra", "page": 1, "max_review_pages": 5,
             "today": "2026-08-30"}
    follow = adapter.follow_tasks(result1.doc, account, meta1)
    assert len(follow) == 1
    assert "page=2" in follow[0].url

    # pass 2 — follow task flows back through _fetch_one
    result2 = runner._fetch_one(follow[0], None, None)
    second_call = stealth_browser.fetch.call_args_list[1]
    assert "page=2" in second_call[0][0]
    assert result2.doc.body == page2_html


def test_runner_closes_stealth_browser_even_when_collection_raises(tmp_path):
    """close() must happen in a finally — an exception mid-collection must
    not leak the browser."""
    stealth_browser = MagicMock()
    datadome_bypass = MagicMock()
    datadome_bypass.stealth = stealth_browser

    runner = _make_runner(MagicMock(), datadome_bypass)
    runner.db = MagicMock()
    runner.stealth_browser = stealth_browser

    adapter = MagicMock()
    adapter.key = "marketplace_g2"
    account = MagicMock()

    # run()'s per-account handler (try/except around _run_pair) swallows
    # adapter.plan() failures, so raise from the eligibility loop's uncaught
    # cursor lookup to exercise the finally with a genuinely propagating error.
    runner._cursor = MagicMock(side_effect=RuntimeError("boom"))

    try:
        runner.run([adapter], [account], force=True, dry_run=True)
    except RuntimeError:
        pass

    stealth_browser.close.assert_called_once()


def test_g2_fragment_empty_vs_challenge_metadata(tmp_path, caplog):
    """The runner must distinguish three fragment outcomes on the Document:
    'ok' (elv-* review cards parsed), 'empty' (200 but zero review cards —
    a new/quiet product, not a block), and 'challenge' (DataDome interstitial,
    which falls back to the bypass waterfall and returns None)."""
    from src.pipeline.runner import CollectorRunner  # noqa: F401  (uses _make_runner)

    def _runner(body):
        doc = Document(doc_id="d", source="marketplace_g2",
                       url="https://www.g2.com/products/sierra/reviews_and_filters",
                       body=body)
        browser = MagicMock()
        browser.fetch.return_value = FetchResult(ok=True, status=200, doc=doc,
                                                 cached=False, error=None, elapsed_ms=10)
        datadome_bypass = MagicMock()
        datadome_bypass.stealth = browser
        return _make_runner(MagicMock(), datadome_bypass)

    task = FetchTask(source="marketplace_g2", url="https://www.g2.com/products/sierra/reviews",
                     domain="g2.com", meta={"product_slug": "sierra", "page": 1})

    # ok: rendered review card present
    result = _runner(RENDERED)._fetch_one(task, None, None)
    assert result is not None and result.doc is not None
    assert result.doc.g2_state == "ok"

    # empty: 200 page but no elv-* review cards
    empty_html = b"<html><body><div class='reviews-empty'>No reviews yet</div></body></html>"
    result = _runner(empty_html)._fetch_one(task, None, None)
    assert result is not None and result.doc is not None
    assert result.doc.g2_state == "empty"

    # challenge: DataDome interstitial -> falls back to bypass waterfall (None)
    challenge_doc = Document(doc_id="c", source="marketplace_g2",
                             url="https://www.g2.com/products/sierra/reviews_and_filters",
                             body=CHALLENGE)
    browser = MagicMock()
    browser.fetch.return_value = FetchResult(ok=True, status=403, doc=challenge_doc,
                                             cached=False, error=None, elapsed_ms=10)
    datadome_bypass = MagicMock()
    datadome_bypass.stealth = browser
    stub_fetcher = MagicMock()
    stub_fetcher.get.return_value = FetchResult(ok=False, status=403, doc=None,
                                                cached=False, error="HTTP 403", elapsed_ms=1)
    runner = _make_runner(stub_fetcher, datadome_bypass)
    # challenge on the fragment -> the bypass waterfall fallback (fetcher path) runs
    result = runner._fetch_one(task, None, None)
    stub_fetcher.get.assert_called()
    assert result.ok is False
    assert challenge_doc.g2_state == "challenge"


def test_g2_stale_cookies_warning_logged(tmp_path, caplog):
    """A DataDome challenge on the stealth fragment most likely means stale
    session cookies — the runner must log a clear WARNING telling the operator
    to re-export data/g2_cookies.json from a logged-in browser."""
    challenge_doc = Document(doc_id="c", source="marketplace_g2",
                             url="https://www.g2.com/products/sierra/reviews_and_filters",
                             body=CHALLENGE)
    browser = MagicMock()
    browser.fetch.return_value = FetchResult(ok=True, status=403, doc=challenge_doc,
                                             cached=False, error=None, elapsed_ms=10)
    datadome_bypass = MagicMock()
    datadome_bypass.stealth = browser
    runner = _make_runner(MagicMock(), datadome_bypass)

    task = FetchTask(source="marketplace_g2", url="https://www.g2.com/products/sierra/reviews",
                     domain="g2.com", meta={"product_slug": "sierra", "page": 1})

    # loguru does not propagate to stdlib logging; bridge it so caplog sees it.
    from loguru import logger as loguru_logger

    class _PropagateHandler(logging.Handler):
        def emit(self, record):
            logging.getLogger("g2-caplog").handle(record)

    handler_id = loguru_logger.add(_PropagateHandler(), level="WARNING")
    try:
        with caplog.at_level(logging.WARNING, logger="g2-caplog"):
            runner._fetch_one(task, None, None)
    finally:
        loguru_logger.remove(handler_id)

    warning_text = " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert "cookies" in warning_text.lower(), warning_text
    assert "re-export" in warning_text.lower() and "data/g2_cookies.json" in warning_text, warning_text


def test_g2_fragment_records_routing_outcomes(tmp_path):
    """Successful fragment fetches record_solve; challenge pages expire —
    so RouteState's lifetime learning observes real cookie lifetimes."""
    from src.antibot.python.routing import RouteState

    routing = RouteState(path=str(tmp_path / "routing.json"))

    ok_doc = Document(
        doc_id="r1", source="marketplace_g2",
        url="https://www.g2.com/products/sierra/reviews_and_filters",
        body=b"<html><article id='sierra-review-1' ue='track-in-viewport'>x</article></html>",
    )
    stealth_browser = MagicMock()
    stealth_browser.fetch.return_value = FetchResult(
        ok=True, status=200, doc=ok_doc, cached=False, error=None, elapsed_ms=10)
    datadome_bypass = MagicMock()
    datadome_bypass.stealth = stealth_browser

    runner = _make_runner(MagicMock(), datadome_bypass)
    runner.routing = routing
    task = FetchTask(source="marketplace_g2", url="https://www.g2.com/products/sierra/reviews",
                     domain="g2.com", meta={"product_slug": "sierra", "page": 1})
    runner._fetch_one(task, None, None)
    assert routing.decide("g2.com") == "Warm"


def test_g2_fragment_challenge_expires_routing(tmp_path):
    """A challenge fragment expires the domain so decide() reports SkipToSolve."""
    from src.antibot.python.routing import RouteState

    routing = RouteState(path=str(tmp_path / "routing.json"))

    ok_doc = Document(
        doc_id="r1", source="marketplace_g2",
        url="https://www.g2.com/products/sierra/reviews_and_filters",
        body=b"<html><article id='sierra-review-1' ue='track-in-viewport'>x</article></html>",
    )
    challenge_doc = Document(
        doc_id="c1", source="marketplace_g2",
        url="https://www.g2.com/products/sierra/reviews_and_filters",
        body=CHALLENGE,
    )
    stealth_browser = MagicMock()
    stealth_browser.fetch.side_effect = [
        FetchResult(ok=True, status=200, doc=ok_doc, cached=False, error=None, elapsed_ms=10),
        FetchResult(ok=True, status=403, doc=challenge_doc, cached=False, error=None, elapsed_ms=10),
    ]
    datadome_bypass = MagicMock()
    datadome_bypass.stealth = stealth_browser

    runner = _make_runner(MagicMock(), datadome_bypass)
    runner.routing = routing
    task = FetchTask(source="marketplace_g2", url="https://www.g2.com/products/sierra/reviews",
                     domain="g2.com", meta={"product_slug": "sierra", "page": 1})
    runner._fetch_one(task, None, None)   # ok -> record_solve (Warm)
    runner._fetch_one(task, None, None)   # challenge -> expire
    assert routing.decide("g2.com") == "SkipToSolve"
