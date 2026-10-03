# src/sources/xray/ledger.py
"""Append-only UTC JSONL run ledger + per-string stats for the X-ray runner.

One row per executed query ("track which strings produce"). ``ts_utc`` comes
ONLY from the injected ``clock`` callable — this module never reads a real
clock (no ``datetime.now()``/``time.time()`` anywhere), so runs are
replayable and tests are offline-deterministic.

Tolerance rule: one bad line must never crash a run over the ledger —
``load_events`` silently skips blank lines and malformed JSON. The written
line is a fresh ``{**event, "ts_utc": clock()}`` dict; the caller's event
dict is never mutated.

This module does file I/O by design (it IS the ledger writer); the runner
owns fetch/DB I/O. Stats aggregate ``{runs, results, profile_hits,
company_hits, last_ts_utc}`` per ``string_id``; ``last_ts_utc`` is the
lexicographic max of the ISO-UTC stamps (all stamps are UTC), ``None`` when
the string has none. Events without ``string_id`` are skipped by the
aggregator.
"""
from __future__ import annotations

import json
from pathlib import Path

__all__ = ["append_event", "load_events", "string_stats"]


def append_event(path: str | Path, event: dict, *, clock) -> None:
    """Append ONE JSON line to the JSONL ledger, stamped from the injected clock.

    ``clock`` is a zero-arg callable returning an ISO-8601 UTC string
    (e.g. ``lambda: "2026-10-02T12:00:00+00:00"``). The parent directory is
    created on first append; ``event`` itself is never mutated.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamped = {**event, "ts_utc": clock()}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(stamped, ensure_ascii=False) + "\n")


def load_events(path: str | Path) -> list[dict]:
    """Parse every ledger line tolerantly; missing file -> [].

    Blank lines and malformed JSON are skipped silently — one bad line
    (partial write, hand edit) must never crash a run over the ledger.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    events: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def string_stats(events: list[dict]) -> dict[str, dict]:
    """Aggregate per ``string_id``: runs, results, profile_hits, company_hits, last_ts_utc.

    Counters sum over events carrying that ``string_id`` (missing counts
    contribute 0); ``last_ts_utc`` is the max ISO-UTC stamp or ``None``.
    Events without ``string_id`` are skipped.
    """
    stats: dict[str, dict] = {}
    for ev in events:
        string_id = ev.get("string_id")
        if not string_id:
            continue
        s = stats.setdefault(string_id, {
            "runs": 0, "results": 0, "profile_hits": 0,
            "company_hits": 0, "last_ts_utc": None,
        })
        s["runs"] += 1
        for key in ("results", "profile_hits", "company_hits"):
            val = ev.get(key)
            # bool is an int subclass — a true/false event value must not count.
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                s[key] += val
        ts = ev.get("ts_utc")
        if isinstance(ts, str) and (s["last_ts_utc"] is None or ts > s["last_ts_utc"]):
            s["last_ts_utc"] = ts
    return stats
