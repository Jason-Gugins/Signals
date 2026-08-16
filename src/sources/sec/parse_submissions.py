"""PURE parsers for SEC submissions JSON."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from src.identity.edgar_ids import pad_cik


class ParseError(ValueError):
    """SEC document could not be parsed."""


@dataclass(frozen=True)
class Filing:
    accession: str
    form: str
    filing_date: str
    report_date: Optional[str]
    items: list[str]
    primary_document: str
    description: Optional[str]
    cik: str = ""

    @property
    def archive_url(self) -> str:
        return archive_url(self.cik, self.accession, self.primary_document)


def archive_url(cik: str, accession: str, document: str) -> str:
    return (
        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
        f"{accession.replace('-', '')}/{document}"
    )


def _col(recent: dict, name: str, n: int) -> list:
    vals = recent.get(name)
    if vals is None:
        return [""] * n
    if len(vals) != n:
        raise ParseError(f"filings.recent.{name} length {len(vals)} != {n}")
    return vals


def parse_submissions(body: bytes) -> tuple[dict, list[Filing]]:
    raw = json.loads(body)
    identity = {}
    for key in (
        "cik",
        "name",
        "tickers",
        "sic",
        "sicDescription",
        "stateOfIncorporation",
        "entityType",
        "website",
        "category",
        "fiscalYearEnd",
    ):
        if key in raw and raw[key] not in (None, "", []):
            identity[key] = pad_cik(raw[key]) if key == "cik" else raw[key]
    files = (raw.get("filings") or {}).get("files") or []
    identity["older_files"] = [f.get("name") for f in files if f.get("name")]
    recent = (raw.get("filings") or {}).get("recent") or {}
    acc = recent.get("accessionNumber") or []
    n = len(acc)
    forms = _col(recent, "form", n)
    fdates = _col(recent, "filingDate", n)
    rdates = _col(recent, "reportDate", n)
    items_col = _col(recent, "items", n)
    pdocs = _col(recent, "primaryDocument", n)
    pdescs = _col(recent, "primaryDocDescription", n)
    cik = identity.get("cik") or pad_cik(raw.get("cik") or "0")
    filings = []
    for i in range(n):
        items_raw = items_col[i] or ""
        items = [x.strip() for x in str(items_raw).replace(";", ",").split(",") if x.strip()]
        rdate = rdates[i] or None
        desc = pdescs[i] or None
        filings.append(
            Filing(
                accession=acc[i],
                form=forms[i],
                filing_date=fdates[i],
                report_date=rdate or None,
                items=items,
                primary_document=pdocs[i],
                description=desc,
                cik=cik,
            )
        )
    filings.sort(key=lambda f: (f.filing_date, f.accession), reverse=True)
    return identity, filings


def filings_since(filings: list[Filing], cursor: str | None, forms: set[str]) -> list[Filing]:
    newest_first = sorted(filings, key=lambda f: (f.filing_date, f.accession), reverse=True)
    if cursor:
        idx = next((i for i, f in enumerate(newest_first) if f.accession == cursor), None)
        if idx is not None:
            newest_first = newest_first[:idx]
    return [f for f in newest_first if f.form in forms]
