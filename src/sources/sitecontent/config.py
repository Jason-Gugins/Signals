"""sitecontent config from config/sources.yaml.

Mirrors the news serp_config loader pattern: an absolute path (overridable in
tests), yaml.safe_load, and NEVER raising — a missing file or garbled YAML
degrades to the defaults, because the caller runs inside the collect loop.

This module deliberately defines no ``parse`` function: the purity guard scans
modules that define one, and the config reader is honest I/O (it reads the
config file), not parser logic.
"""

from __future__ import annotations

from pathlib import Path

import yaml

DEFAULT_CONTENT_PATTERNS = ["/case-studies/", "/blog/", "/compare/", "/solutions/"]
DEFAULT_MAX_FETCHES_PER_CYCLE = 20
DEFAULT_PROSE_FLOOR_CHARS = 500
DEFAULT_MAX_PROSE_CHARS = 50_000

#: Defaults as one dict (the collector's fallback shape; also the test seam).
DEFAULT_CFG = {
    "content_patterns": list(DEFAULT_CONTENT_PATTERNS),
    "max_fetches_per_cycle": DEFAULT_MAX_FETCHES_PER_CYCLE,
    "prose_floor_chars": DEFAULT_PROSE_FLOOR_CHARS,
    "max_prose_chars": DEFAULT_MAX_PROSE_CHARS,
}

# Absolute path to sources.yaml (overridable in tests).
SOURCES_YAML_PATH = Path(__file__).resolve().parents[3] / "config" / "sources.yaml"


def load_sitecontent_cfg() -> dict:
    """The ``sitecontent`` entry of config/sources.yaml, defaults filled in.

    Keys: ``content_patterns`` (URL substrings that mark a sitemap <loc> as a
    content page — arm B's NO-GO means /blog/ folds in here), ``max_fetches_
    per_cycle`` (workday detail-budget precedent), ``prose_floor_chars`` (a
    page must render at least this much stripped prose to earn its
    content_appearance marker) and ``max_prose_chars`` (extraction cap).
    """
    out = {
        "content_patterns": list(DEFAULT_CONTENT_PATTERNS),
        "max_fetches_per_cycle": DEFAULT_MAX_FETCHES_PER_CYCLE,
        "prose_floor_chars": DEFAULT_PROSE_FLOOR_CHARS,
        "max_prose_chars": DEFAULT_MAX_PROSE_CHARS,
    }
    try:
        table = yaml.safe_load(Path(SOURCES_YAML_PATH).read_text(encoding="utf-8")) or {}
        entry = (table.get("sources") or {}).get("sitecontent") or {}
    except Exception:
        return out
    patterns = entry.get("content_patterns")
    if isinstance(patterns, list) and patterns:
        out["content_patterns"] = [str(p) for p in patterns]
    for key in ("max_fetches_per_cycle", "prose_floor_chars", "max_prose_chars"):
        try:
            value = int(entry.get(key, out[key]) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            out[key] = value
    return out
