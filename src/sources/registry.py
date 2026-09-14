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


def enabled_sources(
    config: Config, *, include_disabled: set[str] | None = None
) -> list[SourceAdapter]:
    """Return the instantiated source adapters enabled for this config.

    Entries are included when the configured ``enabled`` value is true, OR when
    the exact key is listed in ``include_disabled`` (an explicit per-key opt-in
    used by the intel command). The registration check and the tier=='browser'
    master-switch check still apply to every entry, so ``include_disabled`` can
    never bypass ``config.browser.enabled``. ``include_disabled`` does NOT mean
    "all disabled sources".
    """
    include = include_disabled or set()
    table = config.load_yaml("sources")
    entries = table.get("sources", table)
    defaults = table.get("defaults") or {}
    out: list[SourceAdapter] = []
    for key, entry in sorted((entries or {}).items()):
        if not isinstance(entry, dict):
            continue
        enabled = entry.get("enabled", defaults.get("enabled", True))
        if not enabled and key not in include:
            continue
        cls = SOURCES.get(key)
        if cls is None:
            continue
        if cls.tier == "browser" and not config.browser.enabled:
            logger.warning("skipping browser-tier source {} (browser.enabled=false)", key)
            continue
        out.append(cls())
    return out


ATS_PREFIX = "ats_"
# Careers-page fallback adapter: fires ONLY for accounts with no real ATS
# (both ats_vendor and ats_token empty) — mutually exclusive with the
# vendor-matched boards below, so the same open roles are never harvested
# twice (no duplicate job rows).
_CAREERS_FALLBACK_KEY = "ats_careers_page"
COLLECTED_VENDORS = {
    "greenhouse",
    "lever",
    "ashby",
    "smartrecruiters",
    "workable",
    "recruitee",
    "workday",
    "rippling",
    "jobvite",
    "breezy",
    "teamtailor",
}


def _ats_vendor_ok(adapter: SourceAdapter, account: Account) -> bool:
    if not adapter.key.startswith(ATS_PREFIX):
        return True
    if adapter.key == _CAREERS_FALLBACK_KEY:
        # Special case: the generic careers scrape runs only when the
        # account has NO ATS at all (no vendor, no token).
        return not (account.ats_vendor or "").strip() and not (
            account.ats_token or ""
        ).strip()
    vendor = (account.ats_vendor or "").casefold()
    if vendor not in COLLECTED_VENDORS:
        return False
    return adapter.key == f"{ATS_PREFIX}{vendor}"


def sources_for_account(account: Account, adapters: list[SourceAdapter]) -> list[SourceAdapter]:
    ready = []
    for adapter in adapters:
        missing = False
        for field in adapter.requires:
            val = getattr(account, field, None)
            if val is None or val == "":
                missing = True
                break
        if not missing and _ats_vendor_ok(adapter, account):
            ready.append(adapter)
    return ready
