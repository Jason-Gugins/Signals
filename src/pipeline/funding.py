"""Form D funding tracker planners. plan() is pure."""

from __future__ import annotations

from datetime import date, timedelta

from src.identity.edgar_ids import SUBMISSIONS_URL, pad_cik
from src.sources.base import FetchTask
from src.sources.sec.fts import fts_search_url

SOURCE = "sec_formd"


def plan_funding(
    mode: str,
    *,
    today: date,
    q: str | None = None,
    cik: str | None = None,
    days: int = 30,
    forms: tuple[str, ...] = ("D",),
    offset: int = 0,
    size: int = 100,
    limit: int = 100,
) -> list[FetchTask]:
    today_s = today.isoformat()
    if mode == "recent":
        start = (today - timedelta(days=days)).isoformat()
        url = fts_search_url(forms=forms, start=start, end=today_s, offset=offset, size=size)
        return [
            FetchTask(
                source=SOURCE,
                url=url,
                domain=None,
                meta={
                    "kind": "fts",
                    "mode": "recent",
                    "today": today_s,
                    "limit": limit,
                    "size": size,
                    "offset": offset,
                },
            )
        ]
    if mode == "search":
        if not q:
            raise ValueError("search requires q")
        start = (today - timedelta(days=days)).isoformat()
        url = fts_search_url(q=q, forms=forms, start=start, end=today_s, offset=offset, size=size)
        return [
            FetchTask(
                source=SOURCE,
                url=url,
                domain=None,
                meta={
                    "kind": "fts",
                    "mode": "search",
                    "today": today_s,
                    "limit": limit,
                    "size": size,
                    "offset": offset,
                },
            )
        ]
    if mode == "company":
        if cik:
            cik10 = pad_cik(cik)
            return [
                FetchTask(
                    source=SOURCE,
                    url=SUBMISSIONS_URL.format(cik10=cik10),
                    domain=None,
                    meta={"kind": "submissions", "mode": "company", "cik": cik10, "today": today_s, "limit": limit},
                )
            ]
        if q:
            quoted = q if q.startswith('"') else f'"{q}"'
            url = fts_search_url(q=quoted, forms=forms, offset=offset, size=size)
            return [
                FetchTask(
                    source=SOURCE,
                    url=url,
                    domain=None,
                    meta={
                        "kind": "fts",
                        "mode": "company",
                        "today": today_s,
                        "limit": limit,
                        "size": size,
                        "offset": offset,
                    },
                )
            ]
        raise ValueError("company requires cik or q")
    raise ValueError(f"unknown mode {mode!r}")
