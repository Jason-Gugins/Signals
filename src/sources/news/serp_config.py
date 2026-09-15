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


def load_company_feed_cfg() -> dict:
    """company_feed options from config/sources.yaml (article_follow_max, default 5).

    ``article_follow_max`` caps how many article links behind a company's own
    blog feed the adapter follows in one cycle (the feed carries teaser
    summaries only; the article bodies are where the company's own
    operational-need language lives). ``0`` disables following entirely.

    Never raises: a missing file, a non-numeric override, or garbled YAML
    (yaml.YAMLError — not an OSError) must degrade to the default, because the
    caller runs inside the collect loop.
    """
    try:
        path = Path(SOURCES_YAML_PATH)
        table = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        entry = (table.get("sources") or {}).get("company_feed") or {}
        return {"article_follow_max": int(entry.get("article_follow_max", 5) or 0)}
    except Exception:
        return {"article_follow_max": 5}
