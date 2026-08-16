"""Tests for BrowserFetcher config helpers. Does not launch Chromium."""

from __future__ import annotations

import pytest

from src.core.browser import BrowserDisabled, BrowserFetcher
from src.core.config import BrowserConfig, Config
from src.core.db import Database
from src.core.rawstore import RawStore


def test_fetch_raises_when_browser_disabled(tmp_path):
    cfg = Config()
    cfg.browser = BrowserConfig(enabled=False)
    db = Database(tmp_path / "signals.db")
    store = RawStore(db, raw_dir=tmp_path / "raw")
    fetcher = BrowserFetcher(cfg, store)
    with pytest.raises(BrowserDisabled):
        fetcher.fetch("https://example.com", source="marketplace_g2")


def test_context_args_include_proxy_when_set():
    cfg = Config()
    cfg.browser.proxy_server = "http://127.0.0.1:8443"
    args = BrowserFetcher._build_context_args(cfg)
    assert args["proxy"] == {"server": "http://127.0.0.1:8443"}


def test_context_args_omit_proxy_when_none():
    cfg = Config()
    cfg.browser.proxy_server = None
    args = BrowserFetcher._build_context_args(cfg)
    assert "proxy" not in args
    assert args["viewport"] == cfg.browser.viewport
    assert args["locale"] == cfg.browser.locale
    assert args["timezone_id"] == cfg.browser.timezone
    assert args["user_agent"] == cfg.browser.user_agent
