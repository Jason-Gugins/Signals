"""Load jobsignals thresholds from config/sources.yaml. No parse() in this file."""

from __future__ import annotations

from pathlib import Path

import yaml


def load_jobsignals_cfg() -> dict:
    path = Path(__file__).resolve().parents[3] / "config" / "sources.yaml"
    table = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entry = (table.get("sources") or {}).get("jobsignals") or {}
    return {k: entry[k] for k in ("surge_min_roles", "surge_window_days") if k in entry}
