"""Per-domain GitHub repo stats state for the momentum signal.

Mirrors marketplace/trend.py's state-file pattern: a small JSON file
(default ``data/github/stats.json``) keyed by account domain, each holding
per-repo dicts ``{full_name: {"stars": int, "archived": bool, "seen_at":
str}}``. The runner loads it, diffs the current repo snapshot via
``community.github.repo_delta``, emits the momentum candidates, then saves
the updated snapshot. Deliberately I/O-only — all signal math is pure.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_STATS_PATH = "data/github/stats.json"


def load_stats(path: str | Path = DEFAULT_STATS_PATH) -> dict:
    """Load the per-domain repo stats state; tolerant of missing/corrupt files."""
    try:
        p = Path(path)
        if not p.exists():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_stats(stats: dict, path: str | Path = DEFAULT_STATS_PATH) -> None:
    """Persist the per-domain repo stats state (atomic: temp file + os.replace)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(stats, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)
