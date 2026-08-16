"""Source adapter registry."""

from __future__ import annotations

from loguru import logger

from src.core.config import Config
from src.core.models import Account
from src.sources.base import SourceAdapter


SOURCES: dict[str, type[SourceAdapter]] = {}


def register(cls: type[SourceAdapter]) -> type[SourceAdapter]:
    SOURCES[cls.key] = cls
    return cls


def get_source(key: str) -> type[SourceAdapter]:
    return SOURCES[key]


def enabled_sources(config: Config) -> list[SourceAdapter]:
    table = config.load_yaml("sources")
    entries = table.get("sources", table)
    defaults = table.get("defaults") or {}
    out: list[SourceAdapter] = []
    for key, cls in sorted(SOURCES.items()):
        entry = entries.get(key) or {}
        enabled = entry.get("enabled", defaults.get("enabled", True))
        if not enabled:
            continue
        if cls.tier == "browser" and not config.browser.enabled:
            logger.warning("skipping browser-tier source {} (browser.enabled=false)", key)
            continue
        out.append(cls())
    return out


def sources_for_account(account: Account, adapters: list[SourceAdapter]) -> list[SourceAdapter]:
    ready = []
    for adapter in adapters:
        missing = False
        for field in adapter.requires:
            val = getattr(account, field, None)
            if val is None or val == "":
                missing = True
                break
        if not missing:
            ready.append(adapter)
    return ready
