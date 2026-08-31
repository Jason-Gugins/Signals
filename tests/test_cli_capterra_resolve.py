"""Tests for resolve --capterra (T5) — mocked fetcher, no network.

The resolver module (src/identity/capterra_resolve.py) was implemented by a
subagent that exhausted its iteration budget before writing tests or
committing; this test file covers the shipped implementation per the plan
spec (2026-08-30_232300-p1-roadmap-execution.md Task 5).
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from src.identity.capterra_resolve import resolve_capterra, resolve_capterra_from_results

# Real-shaped search HTML per the Task-1 spike: product-card anchors with
# /p/<id>/<Slug>/ hrefs; includes an add-on variant (family pollution).
SEARCH_HTML = """
<html><body>
<div data-testid="search-product-card">
  <a data-testid="thumbnail-link" href="/p/19319/JIRA/">
    <span>Jira</span><span>Atlassian</span>
  </a>
</div>
<div data-testid="search-product-card">
  <a data-testid="thumbnail-link" href="/p/10039800/Jira-Backup-and-Restore/">
    <span>Jira Backup and Restore</span>
  </a>
</div>
<div data-testid="search-product-card">
  <a data-testid="thumbnail-link" href="/p/211559/Trello/">
    <span>Trello</span>
  </a>
</div>
</body></html>
"""


def _fake_fetcher(html: str):
    class F:
        def get(self, url, **kwargs):
            return html

    return F()


class TestParseCards:
    def test_parses_product_card_anchors_only(self):
        from src.identity.capterra_resolve import parse_capterra_search_results

        results = parse_capterra_search_results(SEARCH_HTML)
        # Candidates must come from card anchors only: the fixture has exactly
        # 3 cards; the embedded RSC payload must not leak extra segments.
        segments = {r["segment"] for r in results}
        assert segments == {"19319/JIRA", "10039800/Jira-Backup-and-Restore", "211559/Trello"}
        assert all(r.get("name") for r in results)

    def test_segments_from_cards(self):
        from src.identity.capterra_resolve import parse_capterra_search_results

        results = parse_capterra_search_results(SEARCH_HTML)
        segments = [r["segment"] for r in results]
        assert "19319/JIRA" in segments
        assert "211559/Trello" in segments
        # add-on variant is also a card, so it appears — selection filters it
        assert "10039800/Jira-Backup-and-Restore" in segments


class TestSelectionLadder:
    def test_exact_name_match_resolves(self):
        r = resolve_capterra_from_results("jira", [
            {"segment": "19319/JIRA", "name": "Jira"},
            {"segment": "10039800/Jira-Backup-and-Restore", "name": "Jira Backup and Restore"},
        ])
        assert r.status == "resolved" and r.segment == "19319/JIRA"

    def test_normalized_match_case_insensitive(self):
        r = resolve_capterra_from_results("JIRA", [{"segment": "19319/JIRA", "name": "Jira"}])
        assert r.status == "resolved" and r.segment == "19319/JIRA"

    def test_addon_pollution_not_silently_chosen(self):
        """Query 'jira backup' matches only the add-on — that's a legit exact
        match of ITS name, but querying 'jira' must NOT pick the add-on."""
        r = resolve_capterra_from_results("jira backup", [
            {"segment": "19319/JIRA", "name": "Jira"},
            {"segment": "10039800/Jira-Backup-and-Restore", "name": "Jira Backup and Restore"},
        ])
        assert r.status == "resolved" and r.segment == "10039800/Jira-Backup-and-Restore"

    def test_ambiguous_when_query_is_prefix_of_multiple(self):
        """'jira' is a normalized prefix of BOTH products' names — two strong
        candidates, no silent guess."""
        r = resolve_capterra_from_results("jira", [
            {"segment": "19319/JIRA", "name": "Jira Software"},
            {"segment": "10039800/Jira-Backup-and-Restore", "name": "Jira Backup"},
        ])
        assert r.status == "ambiguous" and len(r.candidates) >= 2

    def test_no_match_when_empty(self):
        r = resolve_capterra_from_results("zzz", [])
        assert r.status == "no_match" and r.segment is None


class TestResolveCapterraMocked:
    def test_end_to_end_with_fake_fetcher(self):
        r = resolve_capterra("jira", fetcher=_fake_fetcher(SEARCH_HTML))
        assert r.status == "resolved" and r.segment == "19319/JIRA"

    def test_fetcher_exception_returns_error_status(self):
        class Boom:
            def get(self, url, **kw):
                raise RuntimeError("network down")

        r = resolve_capterra("jira", fetcher=Boom())
        assert r.status == "error"


class TestCliResolveCapterra:
    """The --capterra flag on `resolve` resolves accounts and appends segments.

    Follows the repo's CLI test pattern (tests/test_cli_pipeline.py): the click
    group callback constructs a REAL Orchestrator, so we patch Orchestrator at
    class level (init no-op) and AccountRegistry methods, not ctx.obj.
    """

    def _setup(self, monkeypatch, accounts):
        from unittest.mock import MagicMock
        from src.pipeline.orchestrator import Orchestrator
        from src.identity.registry import AccountRegistry

        registry = MagicMock()
        registry.list_accounts.return_value = accounts
        monkeypatch.setattr(Orchestrator, "__init__", lambda self, *a, **k: None)
        monkeypatch.setattr(Orchestrator, "registry", registry, raising=False)
        return registry

    def _patch_resolve(self, monkeypatch, results):
        from src.identity import capterra_resolve

        monkeypatch.setattr(capterra_resolve, "resolve_capterra", lambda name: results.pop(0))

    def _account(self, domain="a.com", name="Acme", g2_slug=None):
        from unittest.mock import MagicMock

        acct = MagicMock()
        acct.domain, acct.name, acct.g2_slug = domain, name, g2_slug
        acct.seed_source = None
        return acct

    def _resolved(self, segment):
        from src.identity.capterra_resolve import ResolveResult

        return ResolveResult(status="resolved", segment=segment)

    def _invoke(self, monkeypatch):
        from click.testing import CliRunner
        from src.cli import main

        return CliRunner().invoke(main, ["resolve", "--capterra"])

    def test_appends_segment_to_g2_slug(self, monkeypatch):
        from unittest.mock import MagicMock

        acct = self._account(g2_slug="existing-g2")
        registry = self._setup(monkeypatch, [acct])
        self._patch_resolve(monkeypatch, [self._resolved("19319/JIRA")])
        self._invoke(monkeypatch)
        assert acct.g2_slug == "existing-g2,19319/JIRA"

    def test_does_not_duplicate_existing_segment(self, monkeypatch):
        acct = self._account(g2_slug="19319/JIRA")
        self._setup(monkeypatch, [acct])
        self._patch_resolve(monkeypatch, [self._resolved("19319/JIRA")])
        self._invoke(monkeypatch)
        assert acct.g2_slug == "19319/JIRA"

    def test_ambiguous_exits_nonzero_and_does_not_write(self, monkeypatch):
        from unittest.mock import MagicMock
        from src.identity.capterra_resolve import ResolveResult

        acct = self._account()
        registry = self._setup(monkeypatch, [acct])
        amb = ResolveResult(status="ambiguous", candidates=[{"segment": "19319/JIRA", "name": "Jira"}])
        self._patch_resolve(monkeypatch, [amb])
        result = self._invoke(monkeypatch)
        assert result.exit_code == 1
        # ambiguous must not have written anything
        registry.upsert.assert_not_called()
