"""Form D funding tracker planners. plan() is pure."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from src.core.models import Document
from src.export.csvout import export_funding
from src.identity.domains import root_domain
from src.identity.edgar_ids import SUBMISSIONS_URL, pad_cik
from src.signals.normalize import normalize_batch
from src.sources.base import FetchTask, SignalCandidate
from src.sources.sec.formd_filter import FormDFilter, keep_form_d
from src.sources.sec.formd_identity import attach_form_d_account
from src.sources.sec.fts import fts_search_url, hit_to_filing, next_fts_offset, parse_fts_response, parse_fts_total
from src.sources.sec.parse_formd import FormD, form_d_to_candidates, parse_form_d
from src.sources.sec.parse_submissions import Filing, parse_submissions

SOURCE = "sec_formd"


def homepage_url(domain: str) -> str:
    root = root_domain(domain) or domain
    return f"https://{root}/"


def plan_company_queries(names: list[str], *, today: date, limit: int, size: int = 100) -> list[FetchTask]:
    out: list[FetchTask] = []
    for name in names[:3]:
        if not name:
            continue
        out.extend(plan_funding("company", today=today, q=name, size=size, limit=limit))
    return out


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


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class FundingStats:
    fetched: int = 0
    hits: int = 0
    kept: int = 0
    signals_new: int = 0
    stubs: int = 0
    csv_path: str | None = None
    rows: list = field(default_factory=list)


class FundingTracker:
    def __init__(self, config, db, registry, store, fetcher, signal_store, taxonomy):
        self.config = config
        self.db = db
        self.registry = registry
        self.store = store
        self.fetcher = fetcher
        self.signal_store = signal_store
        self.taxonomy = taxonomy

    def run(
        self,
        mode: str,
        *,
        today: date,
        q: str | None = None,
        cik: str | None = None,
        domain: str | None = None,
        days: int = 30,
        filt: FormDFilter | None = None,
        limit: int = 100,
        persist: bool = True,
        cohort: str | None = "formd",
        dry_run: bool = False,
        force: bool = False,
        size: int = 100,
    ) -> FundingStats:
        del force
        stats = FundingStats()
        filt = filt or FormDFilter()
        if domain and not cik:
            acct = self.registry.get(domain)
            if acct is None:
                return stats
            cik = acct.cik or None
            if not cik:
                q = q or acct.name
            if not cik and not q:
                return stats
        if dry_run:
            tasks = plan_funding(mode, today=today, q=q, cik=cik, days=days, offset=0, size=size, limit=limit)
            stats.hits = len(tasks)
            return stats
        seen = 0
        offset = 0
        while True:
            tasks = plan_funding(mode, today=today, q=q, cik=cik, days=days, offset=offset, size=size, limit=limit)
            page_hits = 0
            follow: list[FetchTask] = []
            last_fts_body = None
            for task in tasks:
                result = self.fetcher.get(task)
                if not result.ok or result.doc is None or not result.doc.body:
                    continue
                stats.fetched += 1
                meta = dict(task.meta or {})
                meta.setdefault("today", today.isoformat())
                meta.setdefault("limit", max(0, limit - seen))
                kind = meta.get("kind")
                if kind == "fts":
                    hits = parse_fts_response(result.doc.body)
                    page_hits = len(hits)
                    stats.hits += page_hits
                    last_fts_body = result.doc.body
                    follow.extend(follow_funding(result.doc, meta, filt=filt))
                elif kind == "submissions":
                    more = follow_funding(result.doc, meta, filt=filt)
                    stats.hits += len(more)
                    follow.extend(more)
                elif kind == "form_d":
                    follow.append(task)
            follow = follow[: max(0, limit - seen)]
            for task in follow:
                result = self.fetcher.get(task)
                if not result.ok or result.doc is None or not result.doc.body:
                    continue
                stats.fetched += 1
                meta = dict(task.meta or {})
                meta.setdefault("today", today.isoformat())
                rows = parse_funding_doc(result.doc, meta, today=today, filt=filt)
                for fd, _filing, cand in rows:
                    account = attach_form_d_account(fd, self.registry, cohort=cohort)
                    cand.domain_override = account.domain
                    result.doc.domain = account.domain
                    if persist and result.doc.doc_id:
                        try:
                            self.db.execute(
                                "UPDATE documents SET domain=? WHERE doc_id=?",
                                (account.domain, result.doc.doc_id),
                            )
                        except Exception:
                            pass
                    if persist:
                        valid, _ = normalize_batch(
                            [cand],
                            account=account,
                            source=SOURCE,
                            taxonomy=self.taxonomy,
                            now=_now(),
                            raw_ref=result.doc.doc_id,
                        )
                        new, _ = self.signal_store.upsert_many(valid)
                        stats.signals_new += new
                    stats.kept += 1
                    stats.rows.append(cand)
                    if account.seed_source == "sec_formd" and account.domain.startswith("cik"):
                        stats.stubs += 1
                    seen += 1
                    if seen >= limit:
                        break
            if seen >= limit:
                break
            if last_fts_body is None:
                break
            nxt = next_fts_offset(
                offset=offset,
                size=size,
                batch_len=page_hits,
                total=parse_fts_total(last_fts_body),
            )
            if nxt is None:
                break
            offset = nxt
        if persist and stats.kept:
            out = Path(self.config.storage.export_dir) / "funding.csv"
            stats.csv_path = export_funding(self.db, str(out), cohort=cohort)
        return stats
