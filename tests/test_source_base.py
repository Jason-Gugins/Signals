"""Tests for the source adapter registry and enablement filters."""

from __future__ import annotations

from src.core.config import Config
from src.core.models import Account, Document
from src.sources.base import FetchTask, SignalCandidate, SourceAdapter
from src.sources.registry import (
    SOURCES,
    enabled_sources,
    get_source,
    register,
    sources_for_account,
)


def _cleanup(*keys: str) -> None:
    for k in keys:
        SOURCES.pop(k, None)


def test_register_and_get_source_roundtrip():
    @register
    class Dummy(SourceAdapter):
        key = "dummy_roundtrip"
        tier = "http"
        emits = ("award",)

        def plan(self, account, cursor):
            return [FetchTask(source=self.key, url="https://example.com")]

        def parse(self, doc, account, task_meta):
            return []

    try:
        cls = get_source("dummy_roundtrip")
        assert cls is Dummy
        inst = cls()
        tasks = inst.plan(Account(domain="a.com"), None)
        assert tasks[0].url == "https://example.com"
        assert inst.next_cursor(
            Document(doc_id="x", source="dummy_roundtrip"),
            [SignalCandidate(signal_type="award", observed_at="2026-08-01", natural_key="1")],
        ) == "2026-08-01"
    finally:
        _cleanup("dummy_roundtrip")


def test_enabled_sources_respects_yaml_and_browser_gate(monkeypatch, tmp_path):
    @register
    class HttpSrc(SourceAdapter):
        key = "dummy_http"
        tier = "http"

        def plan(self, account, cursor):
            return []

        def parse(self, doc, account, task_meta):
            return []

    @register
    class BrowserSrc(SourceAdapter):
        key = "dummy_browser"
        tier = "browser"

        def plan(self, account, cursor):
            return []

        def parse(self, doc, account, task_meta):
            return []

    @register
    class DisabledSrc(SourceAdapter):
        key = "dummy_off"
        tier = "http"

        def plan(self, account, cursor):
            return []

        def parse(self, doc, account, task_meta):
            return []

    try:
        cfg = Config()
        cfg.browser.enabled = False

        def fake_yaml(name: str):
            return {
                "sources": {
                    "dummy_http": {"enabled": True},
                    "dummy_browser": {"enabled": True},
                    "dummy_off": {"enabled": False},
                }
            }

        monkeypatch.setattr(cfg, "load_yaml", fake_yaml)
        keys = {s.key for s in enabled_sources(cfg)}
        assert keys == {"dummy_http"}

        cfg.browser.enabled = True
        keys_on = {s.key for s in enabled_sources(cfg)}
        assert keys_on == {"dummy_http", "dummy_browser"}
    finally:
        _cleanup("dummy_http", "dummy_browser", "dummy_off")


def test_sources_for_account_filters_requires():
    class NeedsToken(SourceAdapter):
        key = "ats_x"
        tier = "http"
        requires = ("ats_token",)

        def plan(self, account, cursor):
            return []

        def parse(self, doc, account, task_meta):
            return []

    class Always(SourceAdapter):
        key = "news"
        tier = "http"
        requires = ()

        def plan(self, account, cursor):
            return []

        def parse(self, doc, account, task_meta):
            return []

    adapters = [NeedsToken(), Always()]
    bare = Account(domain="a.com")
    ready = Account(domain="a.com", ats_token="acme")
    assert [a.key for a in sources_for_account(bare, adapters)] == ["news"]
    assert [a.key for a in sources_for_account(ready, adapters)] == ["ats_x", "news"]
