"""OH WARN listing-only parser (static link-list HTML).

Source: https://jfs.ohio.gov/warn/ (current + per-year CMS pages).
Listing-only scope per P3 research: pages provide company name + county +
link to a per-notice PDF. Notice dates, effective dates, and affected counts
live only in the PDFs — detail parsing is deferred; those fields are None.
"""

from __future__ import annotations

import re

from lxml import html

from src.sources.warn import WarnNotice

_COUNTY = re.compile(r"[-–—]\s*(?P<county>[A-Za-z .']+?)\s+County\s*$")


class OhWarn:
    code = "OH"
    index_url = "https://jfs.ohio.gov/warn/"
    fmt = "html"

    def discover(self, body: bytes) -> list[str]:
        return []

    def parse(self, body: bytes) -> list[WarnNotice]:
        if not body:
            return []
        doc = html.fromstring(body)
        out: list[WarnNotice] = []
        for a in doc.xpath("//a[@href]"):
            text = (a.text_content() or "").strip()
            href = a.get("href") or ""
            if not href:
                continue
            m = _COUNTY.search(text)
            county = m.group("county").strip() if m else None
            company = text[: m.start()].strip(" -–—") if m else text
            if not company:
                continue  # malformed row: missing company -> skip
            out.append(
                WarnNotice(
                    company_raw=company,
                    state="OH",
                    notice_date=None,  # deferred: PDF detail parsing
                    effective_date=None,
                    affected=None,
                    location=county,
                    url=href,
                    reason=None,
                )
            )
        return out
