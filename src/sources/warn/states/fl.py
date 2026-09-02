"""FL WARN (reactwarn) server-rendered HTML table parser.

Source: https://reactwarn.floridajobs.org/WarnList/Records?year=YYYY (&page=N).
Layout verified by P3 live-probe research: "Company Name" cell is a
concatenation of company, street, and city — split with a trailing-address
regex; street/city are not individually separable, so city is stored in
`location` when it can be recovered from the tail of the cell.
"""

from __future__ import annotations

import re

from lxml import html

from src.core.textutil import to_iso_date
from src.sources.warn import WarnNotice, parse_affected

# company, street, city -> capture company (front) and city (after last " St|Ave|Blvd|Rd|Dr|Ln|Way|Pkwy|Hwy|Ct| Blvd...,")
_ADDR_TAIL = re.compile(
    r"^(?P<company>.+?),\s*(?P<street>[^,]+),\s*(?P<city>.+)$"
)
_MDY = re.compile(r"^(?P<m>\d{1,2})/(?P<d>\d{1,2})/(?P<y>\d{4})$")


def _fl_date(raw: str | None) -> str | None:
    """reactwarn renders dates as MM/DD/YYYY; convert for to_iso_date."""
    if not raw:
        return None
    m = _MDY.match(raw.strip())
    if not m:
        return to_iso_date(raw)
    return f"{m.group('y')}-{int(m.group('m')):02d}-{int(m.group('d')):02d}"


class FlWarn:
    code = "FL"
    index_url = "https://reactwarn.floridajobs.org/WarnList/Records"
    fmt = "html"

    def discover(self, body: bytes) -> list[str]:
        return []

    @staticmethod
    def _split_company(cell: str) -> tuple[str, str | None]:
        m = _ADDR_TAIL.match(cell)
        if not m:
            company = cell.strip()
            city = None
        else:
            company = m.group("company").strip()
            city = m.group("city").strip() or None
        if not re.match(r"[A-Za-z0-9]", company or ""):
            return "", None  # no actual company name in cell
        return company, city

    def parse(self, body: bytes) -> list[WarnNotice]:
        if not body:
            return []
        doc = html.fromstring(body)
        rows = doc.xpath("//table//tr")
        out: list[WarnNotice] = []
        for tr in rows:
            cells = [c.text_content().strip() for c in tr.xpath("./td")]
            if not cells:
                continue  # header row or non-data row
            company_raw, city = self._split_company(cells[0])
            if not company_raw:
                continue  # malformed row: missing company -> skip
            out.append(
                WarnNotice(
                    company_raw=company_raw,
                    state="FL",
                    notice_date=_fl_date(cells[1]) if len(cells) > 1 else None,
                    effective_date=_fl_date(cells[2]) if len(cells) > 2 else None,
                    affected=parse_affected(cells[3]) if len(cells) > 3 else None,
                    location=city,
                    url=None,
                    reason=cells[4] if len(cells) > 4 else None,
                )
            )
        return out
