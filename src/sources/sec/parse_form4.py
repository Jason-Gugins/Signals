"""PURE Form 4 (ownership XML) parser -> insider_trade candidates.

Form 4 primary documents on EDGAR are XML instance documents of the ODE
"ownershipDocument" schema. Real filings ship BOTH namespaced (default
xmlns="http://www.sec.gov/edgar/document/nineoxe") and namespace-free
variants, so all matching is namespace-agnostic: tags are compared by local
name (the ``{uri}tag`` prefix ElementTree adds is stripped).

Aggregation contract: ONE candidate per filing. Across the filing's
non-derivative transactions, ANY disposition flips the net ``tx_type`` to
``"sold"`` (an insider selling part of what they exercised is still a sale to
a prospect) and the displayed share count is the disposed total; a pure
acquisition filing is ``"bought"`` with the acquired total. Price, security
and codes are taken from the dominant direction when mixed.

Malformed XML, missing tables, or unparsable shares fail OPEN with ``[]`` —
never raise, never guess. ``today`` is injected (no clock reads, no I/O).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date

from src.sources.base import SignalCandidate

CONFIDENCE = 0.6

# Transaction-code fallbacks, used ONLY when the A/D element is missing:
# S=sale, F=tax-withholding are dispositions; P=purchase, M=option exercise,
# A=grant/award are acquisitions. G (gift) is deliberately ambiguous and
# therefore NOT inferred.
_DISPOSE_CODES = {"S", "F"}
_ACQUIRE_CODES = {"P", "M", "A"}


def _local(tag) -> str:
    """Namespace-agnostic tag name: ``{uri}ownershipDocument`` -> ``ownershipDocument``."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _findall(el, name: str) -> list:
    if el is None:
        return []
    return [c for c in el.iter() if _local(c.tag) == name]


def _first(el, name: str):
    hits = _findall(el, name)
    return hits[0] if hits else None


def _text_of(el) -> str | None:
    if el is None:
        return None
    text = (el.text or "").strip()
    return text or None


def _value(el, name: str) -> str | None:
    """Text of a child field. ODE fields wrap payloads in <value>; a few
    (transactionCode, rptOwnerName, issuerName) carry it as direct text."""
    node = _first(el, name)
    if node is None:
        return None
    return _text_of(_first(node, "value")) or _text_of(node)


def _shares(txt: str | None):
    """Parse a share count ("1,500", "1500", "1500.0"); None if unparsable."""
    if not txt:
        return None
    try:
        n = float(txt.replace(",", "").strip())
    except ValueError:
        return None
    return int(n) if n.is_integer() else n


def _ad_code(row) -> str | None:
    """Acquired( A )/Disposed( D ) code, falling back to the transaction code."""
    ad = (_value(row, "transactionAcquiredDisposedCode") or "").strip().upper()
    if ad in {"A", "D"}:
        return ad
    code = (_value(row, "transactionCode") or "").strip().upper()
    if code in _DISPOSE_CODES:
        return "D"
    if code in _ACQUIRE_CODES:
        return "A"
    return None


def _fmt_shares(n) -> str:
    return f"{n:,}"


def parse_form4(
    body: bytes,
    *,
    accession: str,
    today: date,
    url: str | None = None,
) -> list[SignalCandidate]:
    """Aggregate one Form 4 ownership XML into one insider_trade candidate.

    Returns [] for anything unparseable or table-less (fail-open).
    """
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []
    rows = _findall(root, "nonDerivativeTransaction")
    if not rows:
        return []

    owners = [
        name
        for name in (_text_of(_first(ro, "rptOwnerName")) for ro in _findall(root, "reportingOwner"))
        if name
    ]
    issuer = _text_of(_first(_first(root, "issuer"), "issuerName"))

    txs: list[dict] = []
    for row in rows:
        shares = _shares(_value(row, "transactionShares"))
        if shares is None:
            continue  # unparsable row — skip, don't guess
        txs.append(
            {
                "security": _value(row, "securityTitle"),
                "shares": shares,
                "price": _value(row, "transactionPricePerShare"),
                "ad": _ad_code(row),
                "code": (_value(row, "transactionCode") or "").strip().upper() or None,
            }
        )
    if not txs:
        return []

    disposed = [t for t in txs if t["ad"] == "D"]
    acquired = [t for t in txs if t["ad"] == "A"]
    if disposed:
        dominant, tx_type, shares = disposed, "sold", sum(t["shares"] for t in disposed)
    elif acquired:
        dominant, tx_type, shares = acquired, "bought", sum(t["shares"] for t in acquired)
    else:
        return []  # rows exist but no direction could be established

    def _dominant_or_last(field: str) -> str | None:
        for t in reversed(dominant):
            if t[field]:
                return t[field]
        for t in reversed(txs):
            if t[field]:
                return t[field]
        return None

    person_name = ", ".join(owners) or None
    security = _dominant_or_last("security")
    price = _dominant_or_last("price")
    codes: list[str] = []
    for t in txs:
        if t["code"] and t["code"] not in codes:
            codes.append(t["code"])
    shares_display = _fmt_shares(shares)

    return [
        SignalCandidate(
            signal_type="insider_trade",
            observed_at=today.isoformat(),
            natural_key=f"insider:{accession}",
            title=f"{person_name or 'Insider'} {tx_type} {shares_display} {security or 'shares'}",
            url=url,
            confidence=CONFIDENCE,
            evidence_data={
                "person_name": person_name,
                "tx_type": tx_type,
                "shares_display": shares_display,
                "security": security,
                "price": price,
                "tx_code": ",".join(codes) or None,
                "issuer": issuer,
            },
        )
    ]
