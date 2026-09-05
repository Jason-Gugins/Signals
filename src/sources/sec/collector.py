"""SEC EDGAR submissions collector."""

from __future__ import annotations

from datetime import date
from typing import Optional

from src.core.models import Account, Document
from src.identity.edgar_ids import SUBMISSIONS_URL, pad_cik
from src.sources.base import FetchTask, SignalCandidate, SourceAdapter
from src.sources.registry import register
from src.sources.sec.parse_8k import classify_8k, classify_form, extract_text
from src.sources.sec.parse_form4 import parse_form4
from src.sources.sec.parse_formd import form_d_to_candidates, parse_form_d
from src.sources.sec.parse_submissions import Filing, filings_since, parse_submissions


@register
class SecEdgarSource(SourceAdapter):
    key = "sec_edgar"
    tier = "http"
    cadence_hours = 24
    requires = ("cik",)
    emits = (
        "funding_form_d",
        "ipo_filing",
        "ipo_pricing",
        "ma_acquirer",
        "ma_target",
        "annual_report_10k",
        "exec_hire",
        "exec_departure",
        "layoff",
        "earnings_warning",
        "bankruptcy_signal",
        "contract_terminated",
        "insider_trade",
    )
    # Only forms classify_form maps or follow_tasks fans out (D/D/A, 8-K, 4,
    # SC 13D/G). Unmapped forms (e.g. 10-Q, SC 14D9) are NOT watched: they
    # would be fetched, parsed, and silently dropped. 10-Q XBRL mapping is
    # deferred. Amendment forms ("4/A", "SC 13D/A", "SC 13G/A") stay
    # unwatched — amendment noise, so no fetched-then-dropped rows return.
    WATCH_FORMS = {
        "8-K",
        "10-K",
        "S-1",
        "S-1/A",
        "424B4",
        "D",
        "D/A",
        "25",
        "425",
        "4",
        "SC 13D",
        "SC 13G",
    }
    # Form 4 fanout bound: heavy option-grant calendars can file dozens of
    # Form 4s in one window — cap primary-doc fetches per cycle (newest first).
    FORM4_MAX_FOLLOWS = 10

    def plan(self, account: Account, cursor: Optional[str]) -> list[FetchTask]:
        cik = pad_cik(account.cik)
        url = SUBMISSIONS_URL.format(cik10=cik)
        return [
            FetchTask(
                source=self.key,
                url=url,
                domain=account.domain,
                cursor_key=account.domain,
                meta={"kind": "submissions", "cursor": cursor, "cik": cik},
            )
        ]

    def parse(self, doc: Document, account: Account, task_meta: dict) -> list[SignalCandidate]:
        if not doc.body:
            return []
        kind = (task_meta or {}).get("kind") or "submissions"
        today = date.fromisoformat(task_meta["today"])
        if kind == "submissions":
            _, filings = parse_submissions(doc.body)
            out: list[SignalCandidate] = []
            for f in filings_since(filings, task_meta.get("cursor"), self.WATCH_FORMS):
                out.extend(classify_form(f, today=today))
            return out
        if kind == "form_d":
            filing = Filing(
                accession=task_meta.get("accession") or "unk",
                form="D",
                filing_date=task_meta.get("filing_date") or today.isoformat(),
                report_date=None,
                items=[],
                primary_document="primary_doc.xml",
                description=None,
                cik=task_meta.get("cik") or account.cik or "0",
            )
            return form_d_to_candidates(parse_form_d(doc.body), filing=filing, today=today)
        if kind == "8k":
            filing = Filing(
                accession=task_meta.get("accession") or "unk",
                form="8-K",
                filing_date=task_meta.get("filing_date") or today.isoformat(),
                report_date=None,
                items=list(task_meta.get("items") or []),
                primary_document="8k.htm",
                description=None,
                cik=task_meta.get("cik") or account.cik or "0",
            )
            return classify_8k(filing, extract_text(doc.body), today=today)
        if kind == "form4":
            return parse_form4(
                doc.body,
                accession=task_meta.get("accession") or "unk",
                today=today,
                url=doc.url,
            )
        return []

    def follow_tasks(self, doc: Document, account: Account, task_meta: dict) -> list[FetchTask]:
        if not doc.body:
            return []
        kind = (task_meta or {}).get("kind") or "submissions"
        if kind != "submissions":
            return []
        _, filings = parse_submissions(doc.body)
        out = []
        form4_follows = 0
        for f in filings_since(filings, task_meta.get("cursor"), self.WATCH_FORMS):
            if f.form in {"D", "D/A"}:
                k = "form_d"
            elif f.form == "8-K":
                k = "8k"
            elif f.form == "4":
                if form4_follows >= self.FORM4_MAX_FOLLOWS:
                    continue  # cap: a filing-heavy company cannot flood the queue
                form4_follows += 1
                k = "form4"
            else:
                continue
            out.append(
                FetchTask(
                    source=self.key,
                    url=f.archive_url,
                    domain=account.domain,
                    meta={
                        "kind": k,
                        "cik": f.cik,
                        "accession": f.accession,
                        "filing_date": f.filing_date,
                        "items": f.items,
                    },
                )
            )
        return out
