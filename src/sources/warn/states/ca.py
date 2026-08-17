"""CA WARN CSV/xlsx-shaped parser (CSV fixture)."""

from __future__ import annotations

import csv
import io

from src.core.textutil import to_iso_date
from src.sources.warn import WarnNotice, parse_affected


class CaWarn:
    code = "CA"
    index_url = "https://edd.ca.gov/en/jobs_and_training/Layoff_Services_WARN"
    fmt = "csv"

    def discover(self, body: bytes) -> list[str]:
        return []

    def parse(self, body: bytes) -> list[WarnNotice]:
        text = body.decode("utf-8", "replace")
        reader = csv.DictReader(io.StringIO(text))
        out = []
        for row in reader:
            rec = {(k or "").strip().casefold(): (v or "").strip() for k, v in row.items()}
            out.append(
                WarnNotice(
                    company_raw=rec.get("company") or rec.get("employer") or "",
                    state="CA",
                    notice_date=to_iso_date(rec.get("notice_date") or rec.get("notice date")),
                    effective_date=to_iso_date(rec.get("effective_date") or rec.get("layoff date")),
                    affected=parse_affected(rec.get("affected") or rec.get("employees")),
                    location=rec.get("location") or rec.get("city"),
                    url=None,
                    reason=rec.get("reason"),
                )
            )
        return out
