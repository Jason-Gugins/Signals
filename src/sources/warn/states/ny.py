"""NY WARN HTML table parser."""

from __future__ import annotations

from lxml import html

from src.core.textutil import to_iso_date
from src.sources.warn import WarnNotice, parse_affected


class NyWarn:
    code = "NY"
    index_url = "https://dol.ny.gov/warn-notices"
    fmt = "html"

    def discover(self, body: bytes) -> list[str]:
        return []

    def parse(self, body: bytes) -> list[WarnNotice]:
        doc = html.fromstring(body)
        rows = doc.xpath("//table//tr")
        out = []
        for tr in rows[1:]:
            cells = [c.text_content().strip() for c in tr.xpath("./td")]
            if len(cells) < 4:
                continue
            out.append(
                WarnNotice(
                    company_raw=cells[0],
                    state="NY",
                    notice_date=to_iso_date(cells[1]),
                    effective_date=to_iso_date(cells[2]) if len(cells) > 2 else None,
                    affected=parse_affected(cells[3] if len(cells) > 3 else None),
                    location=cells[4] if len(cells) > 4 else None,
                    url=None,
                    reason=None,
                )
            )
        return out
