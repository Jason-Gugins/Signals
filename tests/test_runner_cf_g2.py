from types import SimpleNamespace
from unittest.mock import MagicMock
from src.core.models import Document
from src.core.http import FetchResult
from src.sources.base import FetchTask


def test_cf_bypass_routes_marketplace_g2(tmp_path):
    """marketplace_g2 tasks should route through cloudflare bypass."""
    from src.pipeline.runner import CollectorRunner

    # Stub fetcher returns a 403 with CF challenge body
    challenge_doc = Document(doc_id="d1", source="marketplace_g2", url="https://www.g2.com/products/slack/reviews",
                            body=b"<html><title>Just a moment...</title></html>")
    stub_fetcher = MagicMock()
    stub_fetcher.get.return_value = FetchResult(
        ok=False, status=403, doc=challenge_doc, cached=False, error="HTTP 403", elapsed_ms=1
    )

    # Stub bypass that records it was called
    bypass = MagicMock()
    outcome = SimpleNamespace(success=False, result=None)
    bypass.attempt.return_value = outcome

    # Stub config
    config = SimpleNamespace(
        cloudflare=SimpleNamespace(enabled=True),
        http=SimpleNamespace(user_agent="test-ua"),
        browser=SimpleNamespace(proxy_server=None),
    )

    runner = CollectorRunner.__new__(CollectorRunner)
    runner.config = config
    runner.db = None
    runner.fetcher = stub_fetcher
    runner.browser = None
    runner.cloudflare_bypass = bypass
    runner.ctx = None

    task = FetchTask(source="marketplace_g2", url="https://www.g2.com/products/slack/reviews",
                     domain="g2.com", meta={"kind": "reviews"})

    result = runner._fetch_one(task, None, None)

    # The bypass should have been called for marketplace_g2
    assert bypass.attempt.called, "cloudflare_bypass.attempt() should be called for marketplace_g2"
