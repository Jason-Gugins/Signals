"""Wayback CDX parsing and snapshot selection. PURE."""

from __future__ import annotations

import json
from urllib.parse import quote


def cdx_url(target: str, *, from_year: int, limit: int = 200) -> str:
    return f"http://web.archive.org/cdx/search/cdx?url={quote(target)}/&output=json&fl=timestamp,original,digest,statuscode&collapse=digest&from={from_year}&limit={limit}"


def parse_cdx(body: bytes) -> list[dict]:
    raw = json.loads(body)
    if not raw:
        return []
    header, rows = raw[0], raw[1:]
    out = []
    for row in rows:
        out.append({header[i]: row[i] for i in range(min(len(header), len(row)))})
    return out


def pick_snapshots(rows: list[dict], *, per_year: int = 2, max_total: int = 12) -> list[str]:
    by_year: dict[str, list[str]] = {}
    for r in rows:
        ts = r.get("timestamp") or ""
        if len(ts) < 4:
            continue
        by_year.setdefault(ts[:4], []).append(ts)
    picked = []
    for year in sorted(by_year, reverse=True):
        stamps = sorted(by_year[year])
        if len(stamps) <= per_year:
            picked.extend(stamps)
        else:
            step = max(1, len(stamps) // per_year)
            picked.extend(stamps[::step][:per_year])
        if len(picked) >= max_total:
            break
    return picked[:max_total]


def snapshot_url(timestamp: str, original: str) -> str:
    return f"https://web.archive.org/web/{timestamp}id_/{original}"
