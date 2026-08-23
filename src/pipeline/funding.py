"""Form D funding tracker planners. plan() is pure."""

from __future__ import annotations

from datetime import date, timedelta

from src.core.models import Document
from src.identity.edgar_ids import SUBMISSIONS_URL, pad_cik
from src.sources.base import FetchTask, SignalCandidate
from src.sources.sec.formd_filter import FormDFilter, keep_form_d
from src.sources.sec.fts import fts_search_url, hit_to_filing, parse_fts_response
from src.sources.sec.parse_formd import FormD, form_d_to_candidates, parse_form_d
from src.sources.sec.parse_submissions import Filing, parse_submissions

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


def follow_funding(doc: Document, task_meta: dict, *, filt: FormDFilter) -> list[FetchTask]:
    if not doc.body:
        return []
    kind = (task_meta or {}).get("kind") or "fts"
    limit = int((task_meta or {}).get("limit") or 100)
    today_s = (task_meta or {}).get("today") or ""
    if kind == "form_d":
        return []
    out: list[FetchTask] = []
    if kind == "fts":
        for hit in parse_fts_response(doc.body):
            filing = hit_to_filing(hit)
            if filing is None:
                continue
            if filing.form == "D/A" and not filt.include_amendments:
                continue
            out.append(_form_d_task(filing, today_s, limit))
            if len(out) >= limit:
                break
        return out
    if kind == "submissions":
        wanted = {"D", "D/A"} if filt.include_amendments else {"D"}
        try:
            _, filings = parse_submissions(doc.body)
        except Exception:
            return []
        for filing in filings:
            if filing.form not in wanted:
                continue
            out.append(_form_d_task(filing, today_s, limit))
            if len(out) >= limit:
                break
        return out
    return []


def _form_d_task(filing: Filing, today_s: str, limit: int) -> FetchTask:
    return FetchTask(
        source=SOURCE,
        url=filing.archive_url,
        domain=None,
        meta={
            "kind": "form_d",
            "cik": filing.cik,
            "accession": filing.accession,
            "filing_date": filing.filing_date,
            "today": today_s,
            "limit": limit,
        },
    )


def parse_funding_doc(
    doc: Document,
    task_meta: dict,
    *,
    today: date,
    filt: FormDFilter,
) -> list[tuple[FormD, Filing, SignalCandidate]]:
    if not doc.body:
        return []
    kind = (task_meta or {}).get("kind") or ""
    if kind != "form_d":
        return []
    try:
        fd = parse_form_d(doc.body)
    except Exception:
        return []
    if not keep_form_d(fd, filt):
        return []
    filing = Filing(
        accession=(task_meta or {}).get("accession") or "unk",
        form="D",
        filing_date=(task_meta or {}).get("filing_date") or today.isoformat(),
        report_date=None,
        items=[],
        primary_document="primary_doc.xml",
        description=None,
        cik=(task_meta or {}).get("cik") or fd.cik or "0",
    )
    cands = form_d_to_candidates(fd, filing=filing, today=today)
    return [(fd, filing, c) for c in cands]
