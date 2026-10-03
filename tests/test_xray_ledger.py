# tests/test_xray_ledger.py
"""Ledger: UTC stamps from injected clock, append/load round-trip, stats."""
from src.sources.xray.ledger import append_event, load_events, string_stats

def test_append_stamps_utc_and_roundtrips(tmp_path):
    p = tmp_path / "ledger.jsonl"
    append_event(p, {"string_id": "s1", "query": "q", "results": 3},
                 clock=lambda: "2026-10-02T12:00:00+00:00")
    evs = load_events(p)
    assert evs[0]["ts_utc"] == "2026-10-02T12:00:00+00:00"
    assert evs[0]["string_id"] == "s1"

def test_string_stats_aggregates(tmp_path):
    p = tmp_path / "ledger.jsonl"
    append_event(p, {"string_id": "s1", "results": 3, "profile_hits": 2, "company_hits": 1}, clock=lambda: "2026-10-02T12:00:00+00:00")
    append_event(p, {"string_id": "s1", "results": 1, "profile_hits": 0, "company_hits": 1}, clock=lambda: "2026-10-03T12:00:00+00:00")
    stats = string_stats(load_events(p))
    s = stats["s1"]
    assert s["runs"] == 2 and s["results"] == 4 and s["profile_hits"] == 2
    assert s["company_hits"] == 2 and s["last_ts_utc"] == "2026-10-03T12:00:00+00:00"

def test_load_events_missing_file_returns_empty(tmp_path):
    assert load_events(tmp_path / "nope.jsonl") == []

def test_load_events_skips_malformed_lines(tmp_path):
    p = tmp_path / "ledger.jsonl"
    p.write_text('{"string_id": "s1"}\nnot json at all\n\n{"string_id": "s2"}\n', encoding="utf-8")
    evs = load_events(p)
    assert [e["string_id"] for e in evs] == ["s1", "s2"]

def test_append_creates_parent_dirs(tmp_path):
    p = tmp_path / "deep" / "nested" / "ledger.jsonl"
    append_event(p, {"string_id": "s1"}, clock=lambda: "2026-10-02T12:00:00+00:00")
    assert p.exists()
