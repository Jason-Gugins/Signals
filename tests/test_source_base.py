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
        key = "needs_token"
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
    assert [a.key for a in sources_for_account(ready, adapters)] == ["needs_token", "news"]


def test_include_disabled_selects_one_explicit_key(monkeypatch):
    @register
    class OffSrc(SourceAdapter):
        key = "dummy_optin"
        tier = "http"

        def plan(self, account, cursor):
            return []

        def parse(self, doc, account, task_meta):
            return []

    @register
    class OnSrc(SourceAdapter):
        key = "dummy_on"
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
                    "dummy_on": {"enabled": True},
                    "dummy_optin": {"enabled": False},
                }
            }

        monkeypatch.setattr(cfg, "load_yaml", fake_yaml)
        assert {s.key for s in enabled_sources(cfg)} == {"dummy_on"}
        keys = {s.key for s in enabled_sources(cfg, include_disabled={"dummy_optin"})}
        assert keys == {"dummy_on", "dummy_optin"}
        # a named key alone must not drag in other disabled entries
        assert {s.key for s in enabled_sources(cfg, include_disabled={"nope"})} == {"dummy_on"}
    finally:
        _cleanup("dummy_optin", "dummy_on")


def test_include_disabled_cannot_bypass_browser_master_switch(monkeypatch):
    @register
    class BrowserOff(SourceAdapter):
        key = "dummy_optin_browser"
        tier = "browser"

        def plan(self, account, cursor):
            return []

        def parse(self, doc, account, task_meta):
            return []

    try:
        cfg = Config()
        cfg.browser.enabled = False

        def fake_yaml(name: str):
            return {"sources": {"dummy_optin_browser": {"enabled": False}}}

        monkeypatch.setattr(cfg, "load_yaml", fake_yaml)
        assert enabled_sources(cfg, include_disabled={"dummy_optin_browser"}) == []
        cfg.browser.enabled = True
        keys = {s.key for s in enabled_sources(cfg, include_disabled={"dummy_optin_browser"})}
        assert keys == {"dummy_optin_browser"}
    finally:
        _cleanup("dummy_optin_browser")


def test_shipped_marketplace_adapters_stay_disabled_by_default():
    from src.sources.registry import enabled_sources as _enabled

    cfg = Config()
    keys = {s.key for s in _enabled(cfg)}
    assert "marketplace_g2" not in keys
    assert "marketplace_capterra" not in keys
    assert "marketplace_trustradius" not in keys
    assert "marketplace_softwareadvice" not in keys
    assert "marketplace_getapp" not in keys
