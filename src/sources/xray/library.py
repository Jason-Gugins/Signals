# src/sources/xray/library.py
"""Load and validate the X-ray string library (config/lists authority)."""
from __future__ import annotations

import yaml

VALID_KINDS = frozenset({"people", "company", "hiring", "intent"})
REQUIRED_FIELDS = ("id", "kind")

class LibraryError(ValueError):
    pass

def _load(path_or_dict):
    if isinstance(path_or_dict, dict):
        return path_or_dict
    with open(path_or_dict, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}

def load_library(path_or_dict, *, kinds: set[str] | None = None) -> list[dict]:
    """Return validated string specs (defaults merged into each entry)."""
    doc = _load(path_or_dict)
    strings = doc.get("strings")
    if not isinstance(strings, list) or not strings:
        raise LibraryError("library must contain a non-empty 'strings' list")
    defaults = doc.get("defaults") or {}
    out: list[dict] = []
    seen: set[str] = set()
    for raw in strings:
        for field in REQUIRED_FIELDS:
            if not raw.get(field):
                raise LibraryError(f"string missing {field}: {raw!r}")
        if raw["kind"] not in VALID_KINDS:
            raise LibraryError(f"unknown kind {raw['kind']!r} on {raw['id']}")
        if raw["id"] in seen:
            raise LibraryError(f"duplicate string id {raw['id']!r}")
        seen.add(raw["id"])
        entry = {**{k: v for k, v in defaults.items() if k != "exclude"},
                 **raw}
        if "exclude" not in entry and defaults.get("exclude"):
            entry["exclude"] = list(defaults["exclude"])
        if kinds is not None and entry["kind"] not in kinds:
            continue
        out.append(entry)
    return out
