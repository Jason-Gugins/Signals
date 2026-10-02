# tests/test_xray_library.py
"""Library loader: schema validation, kind filter, unknown-slot rejection."""
import pytest

from src.sources.xray.library import load_library, LibraryError

def test_loads_shipped_library():
    strings = load_library("config/lists/xray_strings.yaml")
    ids = {s["id"] for s in strings}
    assert {"people_title_city", "hiring_post_role", "recently_funded_niche"} <= ids

def test_kind_filter():
    strings = load_library("config/lists/xray_strings.yaml", kinds={"hiring"})
    assert strings and all(s["kind"] == "hiring" for s in strings)

def test_missing_id_raises():
    bad = {"strings": [{"kind": "people", "site": "x.com"}]}
    with pytest.raises(LibraryError):
        load_library(bad)  # loader accepts dict or path

def test_unknown_kind_raises():
    bad = {"strings": [{"id": "x", "kind": "nope"}]}
    with pytest.raises(LibraryError):
        load_library(bad)

def test_defaults_exclude_merged():
    strings = load_library({"defaults": {"exclude": ["jobs", "preferred"]},
                            "strings": [{"id": "a", "kind": "people", "site": "s"}]})
    assert strings[0].get("exclude") == ["jobs", "preferred"]

def test_no_defaults_no_exclude():
    strings = load_library({"strings": [{"id": "a", "kind": "people", "site": "s"}]})
    assert strings[0].get("exclude") is None
