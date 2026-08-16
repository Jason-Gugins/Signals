"""SEC EDGAR submissions collector."""

from __future__ import annotations

from typing import Optional

from src.core.models import Account, Document
from src.identity.edgar_ids import SUBMISSIONS_URL, pad_cik
from src.sources.base import FetchTask, SignalCandidate, SourceAdapter
from src.sources.registry import register
from src.sources.sec.parse_submissions import filings_since, parse_submissions


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
    )
    WATCH_FORMS = {
        "8-K",
        "10-K",
        "10-Q",
        "S-1",
        "S-1/A",
        "424B4",
        "D",
        "D/A",
        "25",
        "425",
        "SC 14D9",
    }

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
        if kind != "submissions":
            return []
        _, filings = parse_submissions(doc.body)
        cursor = (task_meta or {}).get("cursor")
        watched = filings_since(filings, cursor, self.WATCH_FORMS)
        follow = [
            {
                "url": f.archive_url,
                "form": f.form,
                "accession": f.accession,
                "filing_date": f.filing_date,
                "items": f.items,
                "primary_document": f.primary_document,
            }
            for f in watched
        ]
        # stash follow-ups on the document extra via candidates' evidence
        # runner (Task 50) re-plans; we also expose via a module-level hook
        self.last_follow_urls = [x["url"] for x in follow]
        return []
