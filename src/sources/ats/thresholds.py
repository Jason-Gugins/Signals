"""Load ATS Workday thresholds from config/sources.yaml. No parse() in this file."""

from __future__ import annotations

from pathlib import Path

import yaml


def load_ats_workday_cfg() -> dict:
    """Per-source knobs for ats_workday; defaults are safe when config is absent.

    `detail_follow_max` caps how many job-detail GETs each list page may spawn.
    Never raises: a missing/unreadable/unparseable config falls back to 10 so a
    bad file cannot abort a fetch cycle.
    """
    path = Path(__file__).resolve().parents[3] / "config" / "sources.yaml"
    try:
        table = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {"detail_follow_max": 10}
    if not isinstance(table, dict):
        return {"detail_follow_max": 10}
    entry = (table.get("sources") or {}).get("ats_workday") or {}
    if not isinstance(entry, dict):
        entry = {}
    try:
        cap = int(entry.get("detail_follow_max", 10) or 0)
    except (TypeError, ValueError):
        cap = 10
    return {"detail_follow_max": cap}