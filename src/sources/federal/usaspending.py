"""Pure helpers for the usaspending.gov award-search API (P3 spike: GO).

Live-verified 2026-09-04 (data/probe/p3_spike_api-usaspending-gov-*.json):

- ``POST /api/v2/search/spending_by_award/`` — body needs ``filters``
  (``keywords`` + ``award_type_codes`` + ``time_period``) and ``fields``;
  keyless, no anti-bot, ~1-2s. Response rows key by display-name fields
  ("Award ID", "Recipient Name", "Award Amount", ...). Requesting
  ``"Description"`` adds it to each row.
- ``POST /api/v2/recipient/`` — ``{"keyword": name, "limit": n}`` resolves a
  name to recipient hashes + canonical names (identity backstop; the
  collector's never-guess ladder lives in :func:`match_recipient`).

PURITY NOTE: this module defines no ``parse`` function, so the AST purity
guard (tests/test_source_purity.py) does not scan it — the one clock read
that seeds the trailing-12-month window lives here
(:func:`build_search_body`), keeping collector.plan() clock-free.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

from src.core.textutil import clean_text, to_iso_date

SEARCH_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
RECIPIENT_URL = "https://api.usaspending.gov/api/v2/recipient/"

# Definitive-contract award type codes (A: fixed price, B: cost pricing,
# C: indefinitely priced... — the standard 4-code contract family; the spike
# verified this exact set returns live results).
AWARD_TYPE_CODES = ("A", "B", "C", "D")

# Response rows are keyed by these display names (API requires `fields`).
SEARCH_FIELDS = (
    "Award ID",
    "Recipient Name",
    "Start Date",
    "End Date",
    "Award Amount",
    "Description",
)


def award_search_body(name: str, *, start_date: str, end_date: str, limit: int = 100, page: int = 1) -> dict:
    """Request body for spending_by_award. PURE (dates passed in)."""
    return {
        "filters": {
            "keywords": [name],
            "award_type_codes": list(AWARD_TYPE_CODES),
            "time_period": [{"start_date": start_date, "end_date": end_date}],
        },
        "fields": list(SEARCH_FIELDS),
        "limit": limit,
        "page": page,
    }


def build_search_body(name: str, *, today: date | None = None, window_days: int = 365) -> dict:
    """Trailing-12-month award-search body.

    The only clock read in the federal source lives HERE (this module defines
    no ``parse``, so the purity guard does not scan it); collector.plan()
    calls this without ``today`` and stays clock-free.
    """
    today = today or date.today()
    return award_search_body(
        name,
        start_date=(today - timedelta(days=window_days)).isoformat(),
        end_date=today.isoformat(),
    )


def recipient_search_body(keyword: str, limit: int = 10) -> dict:
    """Request body for the /recipient/ name-resolution endpoint. PURE."""
    return {"keyword": keyword, "limit": limit}


def _to_amount(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace("$", "").replace(",", "").strip())
        except ValueError:
            return None
    return None


def parse_awards(body: bytes | dict) -> list[dict]:
    """Normalize a spending_by_award response into plain award rows. PURE.

    Accepts raw bytes or an already-decoded dict. Rows without an
    ``Award ID`` are dropped (no natural key → nothing to pin a candidate
    on); unknown response keys are ignored. Missing ``Description`` (the
    spike capture did not request it) normalizes to ``None``.
    """
    if isinstance(body, dict):
        data = body
    else:
        if not body:
            return []
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return []
    if not isinstance(data, dict):
        return []
    out: list[dict] = []
    for row in data.get("results") or []:
        if not isinstance(row, dict):
            continue
        award_id = clean_text(row.get("Award ID"))
        if not award_id:
            continue
        out.append(
            {
                "award_id": award_id,
                "recipient_name": clean_text(row.get("Recipient Name")),
                "amount": _to_amount(row.get("Award Amount")),
                "start_date": to_iso_date(row.get("Start Date")),
                "end_date": to_iso_date(row.get("End Date")),
                "description": clean_text(row.get("Description")),
            }
        )
    return out


def match_recipient(recipient_name: str, account_name: str, others: list[str] | None = None) -> str:
    """Never-guess ladder for pinning an award row on an account. PURE.

    Returns ``"match"`` | ``"ambiguous"`` | ``"no_match"``:

    1. casefolded exact equality → ``match`` (even if other rows also
       substring-match — an exact hit is not a guess);
    2. casefolded substring in EITHER direction → ``match``, but only when it
       is UNIQUE within the response: ``others`` carries the remaining
       recipient names, and when the needle substring-matches several
       DISTINCT plausible rows (company names collide — the plan's "Metric"
       precedent) the row is ``ambiguous``;
    3. otherwise ``no_match`` (including empty/missing names).

    Callers must emit candidates for ``match`` rows only.
    """
    needle = (account_name or "").casefold().strip()
    hay = (recipient_name or "").casefold().strip()
    if not needle or not hay:
        return "no_match"
    if needle == hay:
        return "match"
    distinct = {hay}
    for other in others or []:
        folded = (other or "").casefold().strip()
        if folded:
            distinct.add(folded)
    hits = {row for row in distinct if needle in row or row in needle}
    if not hits:
        return "no_match"
    if len(hits) > 1:
        return "ambiguous"
    return "match"


def humanize_amount(amount: float | int | None) -> str | None:
    """Human award size: ``$1.4B`` / ``$1.2M`` / ``$450,000`` bands."""
    if amount is None:
        return None
    value = float(amount)
    if abs(value) >= 1_000_000_000:
        text = f"{value / 1_000_000_000:.1f}".removesuffix(".0")
        return f"${text}B"
    if abs(value) >= 1_000_000:
        text = f"{value / 1_000_000:.1f}".removesuffix(".0")
        return f"${text}M"
    return f"${value:,.0f}"
