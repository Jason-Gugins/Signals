"""Load SERP keyword config for the google_news source from config/sources.yaml.

Mirrors the jobsignals thresholds loader pattern. Returns the list of
keyword modifiers attached to every account's Google News search.
"""

from __future__ import annotations

from pathlib import Path

import yaml

# Default signal-bearing keywords used to augment Google News searches.
DEFAULT_SERP_KEYWORDS = [
    "fundraising", "funding", "series",
    "new leadership", "CEO", "appoints",
    "product launch", "launches", "GTM",
    "acquisition", "acquires", "acquisition target",
]

# Absolute path to sources.yaml (overridable in tests).
SOURCES_YAML_PATH = Path(__file__).resolve().parents[3] / "config" / "sources.yaml"


def load_google_news_cfg() -> dict:
    """Read serp_keywords (and future options) from the google_news source entry.

    Returns at least the ``serp_keywords`` key, defaulting to
    ``DEFAULT_SERP_KEYWORDS`` when the entry or key is absent.
    """
    path = Path(SOURCES_YAML_PATH)
    try:
        table = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError:
        table = {}
    entry = (table.get("sources") or {}).get("google_news") or {}
    out: dict = {}
    out["serp_keywords"] = entry.get("serp_keywords") or list(DEFAULT_SERP_KEYWORDS)
    return out
