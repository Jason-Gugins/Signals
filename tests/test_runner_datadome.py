from types import SimpleNamespace
from unittest.mock import MagicMock
from src.core.models import Document
from src.core.http import FetchResult
from src.sources.base import FetchTask


CHALLENGE_BODY = b"<html><script>var dd={'rt':'c','cid':'x','hsh':'x','t':'fe','s':1,'e':'x','host':'geo.captcha-delivery.com','cookie':'x'}</script><iframe src='https://geo.captcha-delivery.com/captcha/?initialCid=x&hash=x&cid=x&t=fe&referer=x&s=1&e=x'></iframe></html>"
REAL_BODY = b"<html><body><div class='paper'>Real content</div></body></html>"


def test_runner_routes_datadome_to_bypass(tmp_path):
    """When a DataDome challenge is detected, the runner calls datadome_bypass.attempt()."""
    from src.pipeline.runner import CollectorRunner

    challenge_doc = Document(doc_id="d1", source="marketplace_g2", url="https://www.g2.com/products/slack/reviews",
                            body=CHALLENGE_BODY)
    stub_fetcher = MagicMock()
    stub_fetcher.get.return_value = FetchResult(
        ok=False, status=403, doc=challenge_doc, cached=False, error="HTTP 403", elapsed_ms=1
    )

    datadome_bypass = MagicMock()
    from src.sources.techstack.datadome_bypass import DataDomeOutcome
    datadome_bypass.attempt.return_value = DataDomeOutcome(
        success=True, method="curl_cffi", result_body=REAL_BODY,
        cookies=[{"name": "datadome", "value": "cookie"}]
    )

    cf_bypass = MagicMock()
    cf_bypass.attempt.return_value = MagicMock(success=False, result=None, method=None)

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
    runner.browser = None
    runner.cloudflare_bypass = cf_bypass
    runner.datadome_bypass = datadome_bypass
    runner.store = MagicMock()
    runner.ctx = None

    task = FetchTask(source="marketplace_g2", url="https://www.g2.com/products/slack/reviews",
                     domain="g2.com", meta={"kind": "reviews"})

    result = runner._fetch_one(task, None, None)

    assert datadome_bypass.attempt.called, "datadome_bypass.attempt() should be called"
