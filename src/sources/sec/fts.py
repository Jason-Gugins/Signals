"""SEC EDGAR full-text search parser.

EFTS Form D (recon 2026-08-22, 200 JSON):
  GET https://efts.sec.gov/LATEST/search-index
  verified keys: forms, dateRange, startdt, enddt, q, from, size
  size=10 returned a 100-hit page; hits.total is {value, relation}
  forms=D filters to D and D/A
  hit._source keys used: adsh, ciks, form|root_forms, file_date, display_names
"""

from __future__ import annotations

import json
from datetime import date
from urllib.parse import urlencode

from src.core.models import Account
from src.identity.names import normalize_name
from src.sources.base import SignalCandidate

EFTS_URL = "https://efts.sec.gov/LATEST/search-index"


def fts_search_url(
    *,
    q: str | None = None,
    forms: tuple[str, ...] = ("D",),
    start: str | None = None,
    end: str | None = None,
    offset: int = 0,
    size: int = 100,
) -> str:
    params: list[tuple[str, str]] = []
    if q:
        params.append(("q", q))
    if forms:
        params.append(("forms", ",".join(forms)))
    if start and end:
        params.extend(
            [("dateRange", "custom"), ("startdt", start), ("enddt", end)]
        )
    params.extend([("from", str(int(offset))), ("size", str(int(size)))])
    return f"{EFTS_URL}?{urlencode(params)}"


def parse_fts_response(body: bytes) -> list[dict]:
    raw = json.loads(body)
    out = []
    for hit in ((raw.get("hits") or {}).get("hits") or []):
        src = hit.get("_source") or {}
        ciks = src.get("ciks") or []
        out.append(
            {
                "accession": src.get("adsh") or "",
                "cik": ciks[0] if ciks else "",
                "form": src.get("form") or (src.get("root_forms") or [""])[0],
                "filed": src.get("file_date"),
                "display_names": list(src.get("display_names") or []),
            }
        )
    return out


def fts_to_candidates(hits: list[dict], account: Account, *, today: date) -> list[SignalCandidate]:
    want = normalize_name(account.name) if account.name else None
    if not want:
        return []
    out = []
    for hit in hits:
        names = " ".join(hit.get("display_names") or [])
        if want not in (normalize_name(names) or ""):
            # also allow any individual display name
            if not any(want == normalize_name(n) or (normalize_name(n) or "").find(want) >= 0 for n in hit.get("display_names") or []):
                continue
        out.append(
            SignalCandidate(
                signal_type="ma_target",
                observed_at=hit.get("filed") or today.isoformat(),
                natural_key=hit["accession"],
                title=f"Mentioned in {hit.get('form')} {hit.get('accession')}",
                confidence=0.5,
                evidence_data={
                    "filer_cik": hit.get("cik"),
                    "display_names": hit.get("display_names"),
                    "form": hit.get("form"),
                },
            )
        )
    return out
