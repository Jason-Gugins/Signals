"""Runner routing for marketplace_capterra: NORMAL fetch path (no stealth fragment).

Capterra reviews pages are SERVER-RENDERED (Next.js RSC) — all review cards
are present in the raw HTML of ``https://www.capterra.com/p/<id>/<Slug>/reviews/``,
and Cloudflare passes through in a paced session. Capterra tasks therefore flow
through the runner's normal ``fetcher.get`` path — the DataDome stealth-browser
fragment routing stays G2-ONLY.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.core.http import FetchResult
from src.core.models import Account, Document
from src.sources.base import FetchTask

# Minimal server-rendered Capterra reviews page (one review card) — enough for
# extract_capterra_reviews() to find and parse, so follow_tasks plans page 2.
SERVER_RENDERED = (
    b"<html><body>"
    b"<div data-test-id='review-cards-container'>"
    b"<div>"
    b"<div data-testid='Overall Rating-rating'><span>5.0</span></div>"
    b"<div class='typo-0 text-neutral-90'>July 11, 2026</div>"
    b"<span class='typo-20 font-semibold'>Jane Doe</span>"
    b"<h3>Great tool</h3>"
    b"<span>Pros</span><p>Fast and reliable</p>"
    b"</div>"
    b"</div>"
    b"</body></html>"
)
CF_CHALLENGE = b"<html><head><title>Just a moment...</title></head><body></body></html>"


def _make_runner(stub_fetcher, cloudflare_bypass=None, stealth_browser=None):
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
    runner.browser = None
    runner.cloudflare_bypass = cloudflare_bypass
    runner.datadome_bypass = None
    runner.store = MagicMock()
    runner.ctx = None
    runner.stealth_browser = stealth_browser
    return runner


def _capterra_task(url="https://www.capterra.com/p/19319/JIRA/reviews/", page=1):
    return FetchTask(
        source="marketplace_capterra",
        url=url,
        domain="jira.atlassian.com",
        meta={"kind": "reviews", "product_slug": "19319/JIRA", "page": page},
    )


def test_capterra_tasks_flow_through_normal_fetch():
    """marketplace_capterra tasks go through the plain HTTP fetcher — the
    stealth browser is never launched, and the fetched server-rendered doc
    flows into the adapter's parse()."""
    doc = Document(
        doc_id="cap:1", source="marketplace_capterra",
        url="https://www.capterra.com/p/19319/JIRA/reviews/",
        body=SERVER_RENDERED,
    )
    stub_fetcher = MagicMock()
    stub_fetcher.get.return_value = FetchResult(
        ok=True, status=200, doc=doc, cached=False, error=None, elapsed_ms=50
    )
    stealth_browser = MagicMock()
    runner = _make_runner(stub_fetcher, stealth_browser=stealth_browser)

    task = _capterra_task()
    result = runner._fetch_one(task, None, None)

    # The normal fetcher got the task (with the plain /reviews/ URL).
    stub_fetcher.get.assert_called_once()
    assert stub_fetcher.get.call_args[0][0] is task
    # NOT the stealth browser — Capterra is server-rendered, no fragment fetch.
    stealth_browser.fetch.assert_not_called()
    assert result is not None and result.doc is not None
    assert result.doc.body == SERVER_RENDERED

    # The normal-path result doc flows to the adapter's parse().
    from src.sources.marketplace.collector import MarketplaceCapterraSource

    adapter = MarketplaceCapterraSource()
    account = Account(domain="jira.atlassian.com", g2_slug="19319/JIRA")
    meta = {"kind": "reviews", "product_slug": "19319/JIRA", "page": 1,
            "review_lookback_days": 90, "max_review_pages": 3,
            "today": "2026-08-30"}
    cands = adapter.parse(result.doc, account, meta)
    assert len(cands) == 1
    assert cands[0].signal_type == "intent_2nd_marketplace"


def test_capterra_challenge_falls_back_to_bypass():
    """When the normal fetch returns a Cloudflare challenge body, the existing
    CF bypass waterfall handles it (same as techstack/html tasks) — the result
    is replaced by the bypass outcome or carries the unsolved flag."""
    challenge_doc = Document(
        doc_id="cap:cf", source="marketplace_capterra",
        url="https://www.capterra.com/p/19319/JIRA/reviews/",
        body=CF_CHALLENGE,
    )
    stub_fetcher = MagicMock()
    stub_fetcher.get.return_value = FetchResult(
        ok=True, status=403, doc=challenge_doc, cached=False, error=None, elapsed_ms=5
    )
    solved_doc = Document(
        doc_id="cap:ok", source="marketplace_capterra",
        url="https://www.capterra.com/p/19319/JIRA/reviews/",
        body=SERVER_RENDERED,
    )
    bypass = MagicMock()
    bypass.attempt.return_value = SimpleNamespace(
        success=True,
        result=FetchResult(ok=True, status=200, doc=solved_doc,
                           cached=False, error=None, elapsed_ms=100),
    )
    runner = _make_runner(stub_fetcher, cloudflare_bypass=bypass)

    task = _capterra_task()
    result = runner._fetch_one(task, None, None)

    # The bypass waterfall was consulted for the CF challenge.
    bypass.attempt.assert_called_once()
    assert bypass.attempt.call_args.kwargs["source"] == "marketplace_capterra"
    assert result is not None and result.doc is not None
    assert result.doc.body == SERVER_RENDERED
    assert not getattr(result, "_cloudflare_unsolved", False)


def test_capterra_challenge_bypass_failure_flags_unsolved():
    """If the bypass cannot solve the challenge, the result is flagged
    ``_cloudflare_unsolved`` so the collector can record cloudflare-only."""
    challenge_doc = Document(
        doc_id="cap:cf2", source="marketplace_capterra",
        url="https://www.capterra.com/p/19319/JIRA/reviews/",
        body=CF_CHALLENGE,
    )
    stub_fetcher = MagicMock()
    stub_fetcher.get.return_value = FetchResult(
        ok=True, status=403, doc=challenge_doc, cached=False, error=None, elapsed_ms=5
    )
    bypass = MagicMock()
    bypass.attempt.return_value = SimpleNamespace(success=False, result=None)
    runner = _make_runner(stub_fetcher, cloudflare_bypass=bypass)

    result = runner._fetch_one(_capterra_task(), None, None)

    bypass.attempt.assert_called_once()
    assert getattr(result, "_cloudflare_unsolved", False) is True


def test_capterra_follow_passes_reenter_fetch():
    """End-to-end two-pass: after page 1 parses, follow_tasks plans a page=2
    FetchTask which re-enters the NORMAL fetcher with the ?page=2 URL — not
    the stealth browser."""
    doc = Document(
        doc_id="cap:p1", source="marketplace_capterra",
        url="https://www.capterra.com/p/19319/JIRA/reviews/",
        body=SERVER_RENDERED,
    )
    stub_fetcher = MagicMock()
    stub_fetcher.get.return_value = FetchResult(
        ok=True, status=200, doc=doc, cached=False, error=None, elapsed_ms=50
    )
    stealth_browser = MagicMock()
    runner = _make_runner(stub_fetcher, stealth_browser=stealth_browser)

    from src.sources.marketplace.collector import MarketplaceCapterraSource

    adapter = MarketplaceCapterraSource()
    account = Account(domain="jira.atlassian.com", g2_slug="19319/JIRA")

    # pass 1
    task1 = _capterra_task()
    result1 = runner._fetch_one(task1, None, None)
    meta1 = {"kind": "reviews", "product_slug": "19319/JIRA", "page": 1,
             "max_review_pages": 3, "today": "2026-08-30"}
    follow = adapter.follow_tasks(result1.doc, account, meta1)
    assert len(follow) == 1
    assert follow[0].source == "marketplace_capterra"
    assert follow[0].url.endswith("/reviews/?page=2")

    # pass 2 — the follow task flows back through the NORMAL fetcher
    stub_fetcher.reset_mock()
    page2_doc = Document(
        doc_id="cap:p2", source="marketplace_capterra",
        url="https://www.capterra.com/p/19319/JIRA/reviews/?page=2",
        body=SERVER_RENDERED,
    )
    stub_fetcher.get.return_value = FetchResult(
        ok=True, status=200, doc=page2_doc, cached=False, error=None, elapsed_ms=50
    )
    result2 = runner._fetch_one(follow[0], None, None)
    fetched_task = stub_fetcher.get.call_args[0][0]
    assert fetched_task is follow[0]
    assert "page=2" in fetched_task.url
    assert result2.doc.body == SERVER_RENDERED
    # Still no stealth browser on pagination passes.
    stealth_browser.fetch.assert_not_called()
