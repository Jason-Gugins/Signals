from src.core.models import Account
from src.pipeline.runner import _requires_met
from src.sources.ats.collector import GreenhouseSource, LeverSource, WorkdaySource
from src.sources.news.collector import NewsRssSource
from src.sources.registry import sources_for_account


def test_vendor_gate_matches_only_named_board():
    acct = Account(domain="acme.com", ats_vendor="greenhouse", ats_token="acme")
    assert _requires_met(GreenhouseSource(), acct) is True
    assert _requires_met(LeverSource(), acct) is False
    assert _requires_met(WorkdaySource(), acct) is False


def test_token_without_vendor_skips_all_ats():
    acct = Account(domain="acme.com", ats_token="acme")
    assert _requires_met(GreenhouseSource(), acct) is False


def test_detected_but_unsupported_vendor_skips():
    acct = Account(domain="acme.com", ats_vendor="bamboohr", ats_token="acme")
    assert _requires_met(GreenhouseSource(), acct) is False


def test_non_ats_adapter_unchanged():
    acct = Account(domain="acme.com")
    assert _requires_met(NewsRssSource(), acct) is True


def test_sources_for_account_same_gate():
    acct = Account(domain="acme.com", ats_vendor="lever", ats_token="acme")
    ready = sources_for_account(acct, [GreenhouseSource(), LeverSource(), NewsRssSource()])
    assert [a.key for a in ready] == ["ats_lever", "news_rss"]
